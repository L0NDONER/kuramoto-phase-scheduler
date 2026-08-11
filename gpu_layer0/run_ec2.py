"""run_ec2.py — same as run_kaggle.py, plus broadcasting (r, psi) up to
a Layer 1 aggregator / reflex consumer over multicast (layer0_report.py),
node name "gpu". Real hardware, real root access -- unlike Kaggle, this
box's reflex consumer can actually act on what it observes.
"""
import time

from pynvml_telemetry import GpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)
from layer0_report import mcast_out, send_report

NODE_NAME = "gpu"
TARGET_POWER_FRAC = 0.70
REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = 90.0


def main():
    telem = GpuTelemetry()
    print(f"[layer0-{NODE_NAME}] {telem.n_gpus} NVML-visible GPU(s):")
    for s in telem.sample_all():
        print(f"  [{s.index}] {s.name}  power_limit={s.power_limit_w:.0f}W")

    nodes = [Layer0Node(i) for i in range(telem.n_gpus)]
    report_sock = mcast_out()

    n_intervals = max(1, int(TOTAL_DURATION_S / REPORT_INTERVAL_S))
    for tick in range(n_intervals):
        tick_start = time.time()
        samples = telem.sample_all()
        power_fracs = [s.power_w / s.power_limit_w if s.power_limit_w else 0.0
                       for s in samples]
        gains = [gain_from_deviation(pf, TARGET_POWER_FRAC) for pf in power_fracs]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi = order_parameter(nodes)
        send_report(report_sock, NODE_NAME, r, psi)

        parts = " ".join(
            f"gpu{s.index}: {s.power_w:5.1f}W/{s.power_limit_w:.0f}W "
            f"({pf*100:4.1f}%) temp={s.temp_c}C util={s.util_pct}% k={k:5.1f}"
            for s, pf, k in zip(samples, power_fracs, gains)
        )
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  {parts}",
              flush=True)

        # Real elapsed-time pacing -- see run_kaggle.py's comment: relying on
        # NVML call latency to accidentally pace this is not portable, this
        # exact loop blew through 90 "seconds" of intervals in ~4 real
        # seconds on this box's faster NVML round-trip (confirmed live
        # 2026-08-11).
        elapsed = time.time() - tick_start
        remaining = REPORT_INTERVAL_S - elapsed
        if remaining > 0:
            time.sleep(remaining)

    telem.shutdown()


if __name__ == "__main__":
    main()
