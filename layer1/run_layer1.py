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

from layer0_report import mcast_in, mcast_out, parse_report, send_report
from layer1_oscillator import Layer1Node, gain_from_coherence, integrate_interval

REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
STALE_AFTER_S = 5.0   # drop a node's last-known psi if it hasn't reported this long


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


def _drain_reports(in_sock, deadline, latest, last_seen):
    """Reads every Layer 0 report that arrives before deadline, keeping
    only the most recent (r, psi) per node. Skips packets from 'layer1'
    itself -- Layer 1 must not feed its own broadcast back into its own
    target, that would be a self-referential loop, not mean-field
    entrainment."""
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
        if name == "layer1":
            continue
        latest[name] = (r, psi)
        last_seen[name] = time.monotonic()


def main():
    in_sock = mcast_in()
    out_sock = mcast_out()
    node = Layer1Node()

    latest = {}       # node_name -> most recent (r, psi)
    last_seen = {}    # node_name -> monotonic timestamp of that report

    n_intervals = max(1, int(TOTAL_DURATION_S / REPORT_INTERVAL_S))
    for tick in range(n_intervals):
        deadline = time.monotonic() + REPORT_INTERVAL_S
        _drain_reports(in_sock, deadline, latest, last_seen)

        now = time.monotonic()
        for name in [n for n in latest if now - last_seen[n] > STALE_AFTER_S]:
            del latest[name]
            del last_seen[name]

        r1, psi1 = mean_field(latest)
        gain = gain_from_coherence(r1)
        integrate_interval(node, gain, psi1, REPORT_INTERVAL_S)

        # How far Layer 1's own carrier currently sits from the target it's
        # being forced toward -- wrapped to (-pi, pi], not just theta-target.
        tracking_err = ((node.theta - psi1 + math.pi) % (2 * math.pi)) - math.pi
        # Same error in time units: dividing a phase gap by omega (rad/s)
        # converts it into how many seconds Layer 1's carrier is currently
        # lagging (positive) or leading (negative) the mean-field target,
        # not just how many radians apart they are.
        dt_lag_s = tracking_err / node.omega

        send_report(out_sock, "layer1", r1, node.theta, dt_lag_s)

        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] nodes={sorted(latest)} "
              f"r1={r1:.3f} psi1={psi1:+.3f}  theta1={node.theta:+.3f}  "
              f"gain={gain:7.1f}  tracking_err={tracking_err:+.3f}  "
              f"dt_lag={dt_lag_s*1000:+.2f}ms", flush=True)


if __name__ == "__main__":
    main()
