"""run_layer1.py — Layer 1: listens for every live Layer 0 node's
(r_i, psi_i) reports on the wire layer0_report.py already defines
(239.0.0.6:7460), computes the instantaneous mean-field (r1, psi1) from
whichever psi_i values are currently live, then RK4-integrates
Layer1Node's own carrier toward psi1 instead of just snapshotting it and
stopping (see layer1_oscillator.py's docstring for why this is a real
gap being filled, not a rewrite of something that already worked).

Layer 1's own report goes back out on the same wire as node_id 'layer1'
-- the recursive structure layer0_report.py's docstring describes
("whatever subscribes to a Layer 0 report can subscribe to Layer 1's the
same way") continuing one level up. What gets broadcast as Layer 1's
(r, psi): r1 (the real, honestly-computed upstream coherence -- Layer 1
is a single oscillator, so a literal order-parameter over itself would
be a fabricated constant 1.0, not a fixed reference-frame stub) and
theta (Layer 1's own integrated carrier phase, the thing this module
actually adds).
"""
import cmath
import math
import sys
import time

from goal_field import GoalField
from layer0_report import mcast_in, mcast_out, parse_report, send_report
from layer1_oscillator import Layer1Node, gain_from_meta_state, integrate_interval

REPORT_INTERVAL_S = 0.5
# No arg -> run forever (production default, meant for a persistent
# service). Pass an explicit duration for a bounded test run instead.
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else None
STALE_AFTER_S = 5.0   # drop a node's last-known psi if it hasn't reported this long

# Long-horizon accumulator (see goal_field.py) -- alpha derived from a
# chosen time constant, not guessed, same as everywhere else this
# pattern shows up in this codebase.
GOAL_FIELD_TIME_CONSTANT_S = 120.0
GOAL_FIELD_ALPHA = 2.0 / (GOAL_FIELD_TIME_CONSTANT_S / REPORT_INTERVAL_S + 1)
GOAL_FIELD_REPORT_EVERY_S = 30.0

# Display-only annotations on the periodic S report -- neither constant
# is read anywhere else in this file. S_TARGET=1.0 is the mathematical
# ceiling (perfect coherence, r=1 everywhere weighted); S_LOW_THRESHOLD
# just labels the printed line "below"/"at/above" for a human skimming
# logs. No branch anywhere reacts to either value -- see goal_field.py's
# module docstring for why that stays true by design.
S_TARGET = 1.0
S_LOW_THRESHOLD = 0.5


def mean_field(latest):
    """Instantaneous mean-field over currently-known nodes' (r, psi)
    reports, weighted by each node's own r -- same
    z=mean(r_i*e^(i*psi_i)) formula fractal_layer1/layer1_aggregator.py
    already uses and documents ("a node reporting psi with low r itself
    is a less trustworthy phase reading"), not an unweighted mean over
    psi alone. Returns (r1, psi1), or (0.0, 0.0) if nothing has reported
    yet."""
    if not latest:
        return 0.0, 0.0
    z = sum(r * cmath.exp(1j * psi) for r, psi in latest.values()) / len(latest)
    return abs(z), cmath.phase(z)


def aggregate_tracking_err0(latest):
    """Layer 0's own tracking error -- how far each live source's own psi
    sits from ITS OWN reference (theta=0), weighted by that source's own
    r same as mean_field()'s weighting. Deliberately not the same signal
    as psi1 (the *target* Layer 1 tracks): two sources can agree tightly
    with each other (high r1) while both sitting far from Layer 0's own
    reference -- r1 can't see that on its own, this can. Returns 0.0 if
    nothing has reported yet (no evidence of a problem, not proof of
    none)."""
    if not latest:
        return 0.0
    total_r = sum(r for r, _ in latest.values())
    if total_r == 0:
        return 0.0
    return sum(r * abs(psi) for r, psi in latest.values()) / total_r


def _drain_reports(in_sock, deadline, latest, last_seen):
    """Reads every Layer 0 report that arrives before deadline, keeping
    only the most recent (r, psi) per node. Skips packets from any
    'layerN' name, not just 'layer1' itself -- Layer 1 must only
    aggregate Layer 0 sources, never a higher layer's broadcast (layer2,
    or anything above it later). Accepting those would feed a downward
    loop into the hierarchy: Layer 2 tracks Layer 1's mean-field, so
    Layer 1 re-ingesting Layer 2's output would mean Layer 1's target is
    partly derived from itself, one hop removed -- not mean-field
    entrainment over independent sources anymore."""
    while time.monotonic() < deadline:
        try:
            data, _ = in_sock.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        parsed = parse_report(data)
        if parsed is None:
            continue
        name, r, psi, _dt_lag_s, _amplitude = parsed
        if name.startswith("layer"):
            continue
        latest[name] = (r, psi)
        last_seen[name] = time.monotonic()


def main():
    in_sock = mcast_in()
    out_sock = mcast_out()
    node = Layer1Node()

    latest = {}       # node_name -> most recent (r, psi)
    last_seen = {}    # node_name -> monotonic timestamp of that report

    # Meta-state carried across ticks: gain this tick is informed by how
    # tracking_err1 behaved last tick (there's no way to know this tick's
    # tracking_err1 before integrating -- same one-tick-delayed feedback
    # shape as any zero-order-hold telemetry loop in this codebase), so
    # gain_from_meta_state() sees the PREVIOUS delta, computed fresh
    # after this tick's integration for the NEXT tick to use.
    prev_abs_err1 = 0.0
    delta_abs_err1 = 0.0

    goal_field = GoalField(GOAL_FIELD_ALPHA)
    next_goal_report = GOAL_FIELD_REPORT_EVERY_S

    tick = 0
    while TOTAL_DURATION_S is None or tick * REPORT_INTERVAL_S < TOTAL_DURATION_S:
        deadline = time.monotonic() + REPORT_INTERVAL_S
        _drain_reports(in_sock, deadline, latest, last_seen)

        now = time.monotonic()
        for name in [n for n in latest if now - last_seen[n] > STALE_AFTER_S]:
            del latest[name]
            del last_seen[name]

        r1, psi1 = mean_field(latest)
        tracking_err0 = aggregate_tracking_err0(latest)
        gain = gain_from_meta_state(r1, tracking_err0, delta_abs_err1)
        integrate_interval(node, gain, psi1, REPORT_INTERVAL_S)

        # How far Layer 1's own carrier currently sits from the target it's
        # being forced toward -- wrapped to (-pi, pi], not just theta-target.
        tracking_err1 = ((node.theta - psi1 + math.pi) % (2 * math.pi)) - math.pi
        # Same error in time units: dividing a phase gap by omega (rad/s)
        # converts it into how many seconds Layer 1's carrier is currently
        # lagging (positive) or leading (negative) the mean-field target,
        # not just how many radians apart they are.
        dt_lag_s = tracking_err1 / node.omega

        abs_err1 = abs(tracking_err1)
        delta_abs_err1 = abs_err1 - prev_abs_err1
        prev_abs_err1 = abs_err1

        send_report(out_sock, "layer1", r1, node.theta, dt_lag_s)

        # Observational only -- see goal_field.py's module docstring for
        # why this never feeds back into gain/actuation.
        goal_field.update(psi1, r1, dt_lag_s)

        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] nodes={sorted(latest)} "
              f"r1={r1:.3f} psi1={psi1:+.3f}  theta1={node.theta:+.3f}  "
              f"tracking_err0={tracking_err0:+.3f}  gain={gain:7.1f}  "
              f"tracking_err1={tracking_err1:+.3f}  delta_err1={delta_abs_err1:+.4f}  "
              f"dt_lag={dt_lag_s*1000:+.2f}ms", flush=True)

        elapsed_s = tick * REPORT_INTERVAL_S
        if elapsed_s >= next_goal_report:
            next_goal_report += GOAL_FIELD_REPORT_EVERY_S
            scored = goal_field.report()
            S = goal_field.meta_coherence()
            # Label only -- see S_TARGET/S_LOW_THRESHOLD's comment, this
            # string is never branched on.
            s_status = "below" if S < S_LOW_THRESHOLD else "at/above"
            s_annot = f"S={S:.3f} ({s_status} {S_LOW_THRESHOLD:.2f}, target={S_TARGET:.2f})"
            if scored:
                summary = "  ".join(
                    f"{math.degrees(center):+4.0f}deg: r_h={b.r_h:.3f} "
                    f"dt_h={b.dt_h_ms:.2f}ms (n={b.n_samples})"
                    for center, b in scored[:3]
                )
                print(f"[goal_field @ {elapsed_s:.0f}s] {s_annot}  best bands: {summary}",
                      flush=True)
            else:
                print(f"[goal_field @ {elapsed_s:.0f}s] {s_annot}  not enough samples "
                      f"in any band yet", flush=True)

        tick += 1


if __name__ == "__main__":
    main()
