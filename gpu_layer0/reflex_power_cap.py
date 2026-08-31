"""reflex_power_cap.py — separate reflex consumer for gpu_layer0's
signal. Deliberately NOT part of run_ec2.py/layer0_oscillator.py --
per feedback_substrate_not_a_cron_job, the substrate emits a signal,
actuation is always a separate consumer's independent decision.

Two INDEPENDENT trigger signals, corroborating rather than each picking
their own action -- this is the full fractal feedback loop, not just a
local one:

  A. LOCAL: this process's OWN pynvml telemetry read (real measured
     power draw, not derived from the oscillator at all) sustained
     above SAFETY_THRESHOLD_W for SUSTAIN_CHECKS, corroborated by the
     broadcast local r (from run_ec2.py's "gpu" node) being locked --
     same "don't trust one flapping scalar" discipline as reflex.py.

  B. GLOBAL: the broadcast r1 (from layer1_aggregator.py's "layer1"
     node -- Layer 1 reporting upward the same way a Layer 0 node
     does) drops below R1_DROP_THRESHOLD for R1_SUSTAIN_CHECKS,
     independent of this GPU's own local telemetry entirely. The
     swarm overall losing coherence is reason enough to go
     conservative here, even if this node's own numbers look fine --
     that's the point of subscribing to r1 at all, not just r.

Selection logic: how many of {A, B} are currently sustained decides which
candidate action fires, not just whether to act at all -- see
select_action(). Neither signal alone is treated as proof of the worst
case; both together is. Both candidate caps come from this card's own
queried nvmlDeviceGetPowerManagementLimitConstraints() at startup, not a
second hand-picked constant -- SEVERE_CAP_W is the confirmed real minimum
(2026-08-11), MODERATE_CAP_W is the midpoint between that minimum and the
card's current/default limit, whatever that actually is on this hardware.

Actuation only ever escalates (moderate -> severe), never auto-restores a
looser limit -- undoing a cap is a decision with its own real
consequences (thermal/power headroom given back) and isn't something a
reflex should do silently just because signals went quiet; that's a
separate, human-in-the-loop decision.

Usage: python3 reflex_power_cap.py [duration_s]
"""
import sys
import time

from pynvml_telemetry import GpuTelemetry
from layer0_report import mcast_in, parse_report

import pynvml

SAFETY_THRESHOLD_W = 55.0
SUSTAIN_CHECKS = 3
CHECK_INTERVAL_S = 0.5
LOCK_THRESHOLD = 0.99
R1_DROP_THRESHOLD = 0.85   # global coherence considered degraded below this
R1_SUSTAIN_CHECKS = 3
STALE_S = 3.0

# Action levels, ordered by severity -- select_action() picks one of
# these names based on how many independent signals are corroborating,
# actuate() resolves the name to a real watt value queried from the card
# at startup (see main()), never a second guessed constant.
NONE, MODERATE, SEVERE = "none", "moderate", "severe"


def select_action(local_triggered, global_triggered):
    """Both signals sustained together -> SEVERE (worst case, corroborated
    two independent ways). Exactly one sustained -> MODERATE (a real
    signal, but not doubly-confirmed). Neither -> NONE. This is the
    selection logic: which candidate action fires depends on how many
    signals agree, not just on whether any single one crossed its
    threshold."""
    if local_triggered and global_triggered:
        return SEVERE
    if local_triggered or global_triggered:
        return MODERATE
    return NONE


def check_actuation_permission(handle, current_w):
    """Fails fast, before any trigger has a chance to fire, if this
    process can't actually actuate. Confirmed live on the real T4
    (2026-08-11): nvmlDeviceSetPowerManagementLimit raises
    NVMLError_NoPermission under a normal unprivileged user -- previously
    that only surfaced as a raw traceback mid-run, the first time a real
    trigger condition fired, potentially long into a deployment.

    Writes current_w back to itself -- a real call through the exact same
    privileged path actuate() uses later, not a UID==0 proxy check, so
    this is accurate regardless of *how* permission is actually granted
    on a given box (root, a capability, a udev rule, ...). Net effect on
    the card is zero: the limit ends up exactly where it started."""
    try:
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, int(current_w * 1000))
    except pynvml.NVMLError_NoPermission:
        sys.exit(
            "[reflex] FATAL: no permission to call nvmlDeviceSetPowerManagementLimit "
            "-- this process cannot actuate on this card. Run with sudo (confirmed "
            "live 2026-08-11: ec2-user fails, sudo succeeds), or grant equivalent "
            "privilege, before running this reflex for real."
        )


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    telem = GpuTelemetry()
    report_sock = mcast_in()
    handle = telem._handles[0]

    # Real queried constraints, not a second guessed constant -- min_w is
    # the same confirmed value CAP_TO_W used to be (2026-08-11); default_w
    # is whatever this card is actually running at right now.
    min_w, _max_w = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
    min_w /= 1000.0
    default_w = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
    check_actuation_permission(handle, default_w)
    action_watts = {
        MODERATE: (min_w + default_w) / 2.0,
        SEVERE: min_w,
    }

    print(f"[reflex] watching gpu0: LOCAL threshold={SAFETY_THRESHOLD_W}W x{SUSTAIN_CHECKS} "
          f"+ local r>={LOCK_THRESHOLD}  |  GLOBAL r1<{R1_DROP_THRESHOLD} x{R1_SUSTAIN_CHECKS}  "
          f"|  actions: moderate={action_watts[MODERATE]:.1f}W severe={action_watts[SEVERE]:.1f}W",
          flush=True)

    over_count = 0
    r1_under_count = 0
    last_r, last_r_t = 0.0, 0.0
    last_r1, last_r1_t = 0.0, 0.0
    current_level = NONE
    level_rank = {NONE: 0, MODERATE: 1, SEVERE: 2}
    deadline = time.time() + duration_s

    def actuate(level, reason):
        nonlocal current_level
        watts = action_watts[level]
        print(f"[reflex] TRIGGER ({reason}) -- escalating {current_level} -> {level}, "
              f"applying real cap to {watts:.1f}W", flush=True)
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, int(watts * 1000))
        readback = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
        print(f"[reflex] ACTUATED: power limit now {readback}W (real, confirmed readback)",
              flush=True)
        current_level = level

    while time.time() < deadline:
        now = time.time()
        # Drain every pending report, not just one -- two independent
        # broadcasters (gpu + layer1) means packets can arrive faster than
        # a single recvfrom() per CHECK_INTERVAL_S tick would consume them;
        # reading only one per iteration lets the socket backlog grow
        # unboundedly and this reflex ends up acting on stale state that
        # gets staler over time, not just occasionally behind. Confirmed
        # via dry-run (2026-08-11): r1 never advanced past its first
        # received value in an 18s run at this send rate.
        while True:
            try:
                data, _addr = report_sock.recvfrom(64)
            except BlockingIOError:
                break
            parsed = parse_report(data)
            if parsed:
                name, r, _psi, _dt_lag_s, _amplitude = parsed
                if name == "gpu":
                    last_r, last_r_t = r, now
                elif name == "layer1":
                    last_r1, last_r1_t = r, now

        sample = telem.sample_all()[0]
        r_fresh = (now - last_r_t) < STALE_S
        r_locked = r_fresh and last_r >= LOCK_THRESHOLD
        r1_fresh = (now - last_r1_t) < STALE_S

        over_count = over_count + 1 if sample.power_w >= SAFETY_THRESHOLD_W else 0
        r1_under_count = (r1_under_count + 1
                           if r1_fresh and last_r1 < R1_DROP_THRESHOLD else 0)

        local_triggered = over_count >= SUSTAIN_CHECKS and r_locked
        global_triggered = r1_under_count >= R1_SUSTAIN_CHECKS
        action = select_action(local_triggered, global_triggered)

        print(f"[reflex] power={sample.power_w:.1f}W over_count={over_count} "
              f"r={last_r:.3f}({'fresh' if r_fresh else 'stale'}) "
              f"r1={last_r1:.3f}({'fresh' if r1_fresh else 'stale'}) "
              f"r1_under_count={r1_under_count} action={action} level={current_level}",
              flush=True)

        if action != NONE and level_rank[action] > level_rank[current_level]:
            reason = (f"LOCAL: power sustained >={SAFETY_THRESHOLD_W}W for {SUSTAIN_CHECKS} "
                      f"checks AND local r locked (r={last_r:.3f})" if local_triggered else "")
            if global_triggered:
                global_reason = (f"GLOBAL: r1 sustained <{R1_DROP_THRESHOLD} for "
                                  f"{R1_SUSTAIN_CHECKS} checks (r1={last_r1:.3f})")
                reason = f"{reason} AND {global_reason}" if reason else global_reason
            actuate(action, reason)

        time.sleep(CHECK_INTERVAL_S)

    print(f"[reflex] done, level={current_level}", flush=True)


if __name__ == "__main__":
    main()
