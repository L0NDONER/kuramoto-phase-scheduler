"""reflex_power_cap.py — separate reflex consumer for gpu_layer0's
signal. Deliberately NOT part of run_ec2.py/layer0_oscillator.py --
per feedback_substrate_not_a_cron_job, the substrate emits a signal,
actuation is always a separate consumer's independent decision.

Two INDEPENDENT trigger paths, either can cap on its own -- this is the
full fractal feedback loop, not just a local one:

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

Either path applies a real nvmlDeviceSetPowerManagementLimit call --
actual actuation on real hardware, once, then latches (won't keep
re-triggering every interval).

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
CAP_TO_W = 60.0   # this card's real minimum (confirmed via
                  # nvmlDeviceGetPowerManagementLimitConstraints, 2026-08-11)
STALE_S = 3.0


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    telem = GpuTelemetry()
    report_sock = mcast_in()

    print(f"[reflex] watching gpu0: LOCAL threshold={SAFETY_THRESHOLD_W}W x{SUSTAIN_CHECKS} "
          f"+ local r>={LOCK_THRESHOLD}  |  GLOBAL r1<{R1_DROP_THRESHOLD} x{R1_SUSTAIN_CHECKS}",
          flush=True)

    over_count = 0
    r1_under_count = 0
    last_r, last_r_t = 0.0, 0.0
    last_r1, last_r1_t = 0.0, 0.0
    capped = False
    deadline = time.time() + duration_s

    def actuate(reason):
        nonlocal capped
        print(f"[reflex] TRIGGER ({reason}) -- applying real cap to {CAP_TO_W}W", flush=True)
        handle = telem._handles[0]
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, int(CAP_TO_W * 1000))
        readback = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
        print(f"[reflex] ACTUATED: power limit now {readback}W (real, confirmed readback)",
              flush=True)
        capped = True

    while time.time() < deadline:
        now = time.time()
        try:
            data, _addr = report_sock.recvfrom(64)
            parsed = parse_report(data)
            if parsed:
                name, r, _psi = parsed
                if name == "gpu":
                    last_r, last_r_t = r, now
                elif name == "layer1":
                    last_r1, last_r1_t = r, now
        except BlockingIOError:
            pass

        sample = telem.sample_all()[0]
        r_fresh = (now - last_r_t) < STALE_S
        r_locked = r_fresh and last_r >= LOCK_THRESHOLD
        r1_fresh = (now - last_r1_t) < STALE_S

        over_count = over_count + 1 if sample.power_w >= SAFETY_THRESHOLD_W else 0
        r1_under_count = (r1_under_count + 1
                           if r1_fresh and last_r1 < R1_DROP_THRESHOLD else 0)

        print(f"[reflex] power={sample.power_w:.1f}W over_count={over_count} "
              f"r={last_r:.3f}({'fresh' if r_fresh else 'stale'}) "
              f"r1={last_r1:.3f}({'fresh' if r1_fresh else 'stale'}) "
              f"r1_under_count={r1_under_count} capped={capped}", flush=True)

        if not capped and over_count >= SUSTAIN_CHECKS and r_locked:
            actuate(f"LOCAL: power sustained >={SAFETY_THRESHOLD_W}W for {SUSTAIN_CHECKS} "
                    f"checks AND local r locked (r={last_r:.3f})")
        elif not capped and r1_under_count >= R1_SUSTAIN_CHECKS:
            actuate(f"GLOBAL: r1 sustained <{R1_DROP_THRESHOLD} for {R1_SUSTAIN_CHECKS} "
                    f"checks (r1={last_r1:.3f}) -- swarm coherence degraded, going "
                    f"conservative regardless of local power")

        time.sleep(CHECK_INTERVAL_S)

    print(f"[reflex] done, capped={capped}", flush=True)


if __name__ == "__main__":
    main()
