"""run_layer2.py — Layer 2: listens for Layer 1's (r1, psi1, dt_lag_s)
reports on the wire layer0_report.py already defines (239.0.0.6:7460),
computes the instantaneous mean-field (r2, psi2) over whichever 'layer1'
sources are currently live, then RK4-integrates Layer2Node's own carrier
toward psi2 -- same recursive structure one level further up from
layer1/run_layer1.py, which does the identical thing over Layer 0
sources. See layer0_report.py's docstring: "whatever subscribes to a
Layer 0 report can subscribe to Layer 1's the same way" -- this is that,
continued.

Only ingests reports literally named 'layer1' (currently just one
instance) -- never a raw Layer 0 source name (mint/pi1/pi2/gpu/market)
directly, and never its own 'layer2' broadcast. Skipping raw Layer 0
names is deliberate, not an oversight: Layer 2 sits above Layer 1 in the
hierarchy, so it tracks what Layer 1 already summarized, not the
individual sources underneath it -- bypassing that would flatten the
hierarchy instead of recursing through it.

What gets broadcast as Layer 2's own (r, psi): r2 (the real upstream
coherence among live 'layer1' sources -- honestly computed, not a
fabricated 1.0) and theta (Layer 2's own integrated carrier phase).
"""
import cmath
import math
import sys
import time

from goal_field import GoalField
from layer0_report import mcast_in, mcast_out, parse_report, send_report
from layer2_oscillator import Layer2Node, gain_from_meta_state, integrate_interval

REPORT_INTERVAL_S = 0.5
# No arg -> run forever (production default, meant for a persistent
# service). Pass an explicit duration for a bounded test run instead.
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else None
STALE_AFTER_S = 5.0   # drop a node's last-known psi if it hasn't reported this long

# Long-horizon accumulator (see goal_field.py) -- same time constant as
# layer1/run_layer1.py, alpha derived from it, not guessed.
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
    """Instantaneous mean-field over currently-known 'layer1' sources'
    (r, psi) reports, weighted by each source's own r -- identical
    formula to layer1/run_layer1.py's mean_field(), one level up.
    Returns (r2, psi2), or (0.0, 0.0) if nothing has reported yet."""
    if not latest:
        return 0.0, 0.0
    z = sum(r * cmath.exp(1j * psi) for r, psi in latest.values()) / len(latest)
    return abs(z), cmath.phase(z)


def aggregate_tracking_err1(latest):
    """Layer 1's own tracking error -- how far each live 'layer1'
    source's own psi sits from ITS OWN target, weighted by that source's
    own r. Same role layer1/run_layer1.py's aggregate_tracking_err0()
    plays one level down -- see that function's docstring for the full
    reasoning (why this differs from r2, not repeated here)."""
    if not latest:
        return 0.0
    total_r = sum(r for r, _ in latest.values())
    if total_r == 0:
        return 0.0
    return sum(r * abs(psi) for r, psi in latest.values()) / total_r


def _drain_reports(in_sock, deadline, latest, last_seen):
    """Reads every report that arrives before deadline, keeping only the
    most recent (r, psi) per node -- but only for reports literally named
    'layer1'. Raw Layer 0 names are skipped (see module docstring: Layer
    2 tracks Layer 1's summary, not the sources underneath it), and
    'layer2' itself is skipped for the same self-referential-loop reason
    layer1/run_layer1.py excludes 'layer1' and 'layer2' from its own
    input."""
    while time.monotonic() < deadline:
        try:
            data, _ = in_sock.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        parsed = parse_report(data)
        if parsed is None:
            continue
        name, r, psi, _dt_lag_s = parsed
        if name != "layer1":
            continue
        latest[name] = (r, psi)
        last_seen[name] = time.monotonic()


def main():
    in_sock = mcast_in()
    out_sock = mcast_out()
    node = Layer2Node()

    latest = {}       # node_name -> most recent (r, psi) (only ever 'layer1' today)
    last_seen = {}    # node_name -> monotonic timestamp of that report

    # Meta-state carried across ticks -- same one-tick-delayed feedback
    # shape as layer1/run_layer1.py's prev_abs_err1/delta_abs_err1.
    prev_abs_err2 = 0.0
    delta_abs_err2 = 0.0

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

        r2, psi2 = mean_field(latest)
        tracking_err1 = aggregate_tracking_err1(latest)
        gain = gain_from_meta_state(r2, tracking_err1, delta_abs_err2)
        integrate_interval(node, gain, psi2, REPORT_INTERVAL_S)

        # How far Layer 2's own carrier currently sits from the target
        # it's being forced toward -- wrapped to (-pi, pi].
        tracking_err2 = ((node.theta - psi2 + math.pi) % (2 * math.pi)) - math.pi
        dt_lag_s = tracking_err2 / node.omega

        abs_err2 = abs(tracking_err2)
        delta_abs_err2 = abs_err2 - prev_abs_err2
        prev_abs_err2 = abs_err2

        send_report(out_sock, "layer2", r2, node.theta, dt_lag_s)

        # Observational only -- see goal_field.py's module docstring for
        # why this never feeds back into gain/actuation.
        goal_field.update(psi2, r2, dt_lag_s)

        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] nodes={sorted(latest)} "
              f"r2={r2:.3f} psi2={psi2:+.3f}  theta2={node.theta:+.3f}  "
              f"tracking_err1={tracking_err1:+.3f}  gain={gain:7.1f}  "
              f"tracking_err2={tracking_err2:+.3f}  delta_err2={delta_abs_err2:+.4f}  "
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
