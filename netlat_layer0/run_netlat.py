"""run_netlat.py — Layer 0 driven by real network round-trip latency to
an external host, broadcasting as node "netlat" -- the third,
genuinely independent signal for tonight's multi-consensus test
(alongside "market" and "mint", already broadcasting). Real network
path congestion/routing/remote-host load is a different physical
process from both market price and this machine's own CPU scheduling.

Each tick takes N_NODES sequential real pings (measured ~14ms/ping
including subprocess overhead, so N_NODES=8 fits comfortably inside
REPORT_INTERVAL_S=0.5) instead of one -- a single ping per tick would
give a degenerate ensemble (order_parameter() on N=1 is trivially r=1,
already documented elsewhere in this codebase as not a meaningful
statistic). Deviation is relative ((rtt - target) / target), not raw ms,
so it's on the same normalized scale gain_from_deviation()/K_BASE were
tuned for.
"""
import sys
import time

from netlat_telemetry import sample_rtt_ms
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)
from layer0_report import mcast_out, send_report

NODE_NAME = "netlat"
N_NODES = 8
REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else None


def measure_baseline_target():
    readings = []
    while len(readings) < int(TARGET_MEASURE_S * 2):
        rtt = sample_rtt_ms()
        if rtt is not None:
            readings.append(rtt)
    return sum(readings) / len(readings)


def main():
    print(f"[netlat] calibrating baseline RTT to 1.1.1.1 over ~{TARGET_MEASURE_S:.0f}s...",
          flush=True)
    target_rtt_ms = measure_baseline_target()
    print(f"[netlat] target_rtt={target_rtt_ms:.2f}ms", flush=True)

    nodes = [Layer0Node(i) for i in range(N_NODES)]
    report_sock = mcast_out()

    tick = 0
    start = time.time()
    while TOTAL_DURATION_S is None or time.time() - start < TOTAL_DURATION_S:
        tick_start = time.time()
        rtts = [sample_rtt_ms() for _ in range(N_NODES)]
        devs = [(r - target_rtt_ms) / target_rtt_ms if r is not None else 0.0 for r in rtts]
        gains = [gain_from_deviation(abs(d), 0.0) for d in devs]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi = order_parameter(nodes)
        send_report(report_sock, NODE_NAME, r, psi)

        valid_rtts = [x for x in rtts if x is not None]
        avg_rtt = sum(valid_rtts) / len(valid_rtts) if valid_rtts else float("nan")
        elapsed = tick * REPORT_INTERVAL_S
        print(f"[{elapsed:7.1f}s] r={r:.3f} psi={psi:+.3f}  avg_rtt={avg_rtt:.2f}ms  "
              f"target={target_rtt_ms:.2f}ms", flush=True)

        tick += 1
        # Real elapsed-time pacing, same shape as run_ec2.py -- measured
        # from THIS iteration's own start, not accumulated tick count, so
        # per-tick ping latency variance doesn't compound into drift.
        remaining = REPORT_INTERVAL_S - (time.time() - tick_start)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    main()
