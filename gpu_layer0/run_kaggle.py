"""run_kaggle.py — Layer 0 prototype runner. Copy into a Kaggle notebook
cell (after `pip install pynvml` in the cell before), or run locally on
any machine with an NVML-visible GPU.

Samples real GPU telemetry once per REPORT_INTERVAL_S, derives a per-GPU
coupling gain from how far each GPU's power draw sits from TARGET_POWER_FRAC
of its power limit, RK4-integrates each GPU's phase for that interval, and
reports the order parameter (r, psi) plus the raw telemetry that drove it.

See README.md for what this does and doesn't prove -- mechanics on real
hardware, not real swarm consensus (too few GPUs on a Kaggle session for
r to be a meaningful coherence statistic).
"""
import time

from pynvml_telemetry import GpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)

TARGET_POWER_FRAC = 0.70   # stand-in policy: "run each GPU at 70% of its power budget"
REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = 60.0


def main():
    telem = GpuTelemetry()
    print(f"[layer0] {telem.n_gpus} NVML-visible GPU(s):")
    for s in telem.sample_all():
        print(f"  [{s.index}] {s.name}  power_limit={s.power_limit_w:.0f}W")

    nodes = [Layer0Node(i) for i in range(telem.n_gpus)]

    n_intervals = max(1, int(TOTAL_DURATION_S / REPORT_INTERVAL_S))
    for tick in range(n_intervals):
        samples = telem.sample_all()
        power_fracs = [s.power_w / s.power_limit_w if s.power_limit_w else 0.0
                       for s in samples]
        gains = [gain_from_deviation(pf, TARGET_POWER_FRAC) for pf in power_fracs]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi = order_parameter(nodes)

        parts = " ".join(
            f"gpu{s.index}: {s.power_w:5.1f}W/{s.power_limit_w:.0f}W "
            f"({pf*100:4.1f}%) temp={s.temp_c}C util={s.util_pct}% k={k:5.1f}"
            for s, pf, k in zip(samples, power_fracs, gains)
        )
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  {parts}",
              flush=True)

        time.sleep(0)  # telemetry sampling + integration already took real time

    telem.shutdown()


if __name__ == "__main__":
    main()
