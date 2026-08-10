"""run_local.py — Layer 0 for this machine's (Mint) CPU, same shape as
pi2_cpu_layer0/run_pi2.py, plus broadcasting (r, psi) up to a Layer 1
aggregator over multicast (see layer0_report.py).
"""
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)
from layer0_report import mcast_out, send_report

NODE_NAME = "mint"
TARGET_LOAD_FRAC = 0.50
REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = 60.0


def main():
    telem = CpuTelemetry()
    print(f"[layer0-cpu-{NODE_NAME}] {telem.n_cores} cores", flush=True)

    telem.sample_all()   # discard first sample, no prior state to diff against
    time.sleep(REPORT_INTERVAL_S)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]
    report_sock = mcast_out()

    n_intervals = max(1, int(TOTAL_DURATION_S / REPORT_INTERVAL_S))
    for tick in range(n_intervals):
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains = [gain_from_deviation(l, TARGET_LOAD_FRAC) for l in loads]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi = order_parameter(nodes)
        send_report(report_sock, NODE_NAME, r, psi)

        avg_load = sum(loads) / len(loads) if loads else 0.0
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  "
              f"avg_load={avg_load*100:.1f}%  n_cores={telem.n_cores}", flush=True)

        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
