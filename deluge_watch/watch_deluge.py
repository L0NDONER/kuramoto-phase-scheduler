"""watch_deluge.py — live per-core watcher, no scheduled event: just
observes real, organically-triggered load (e.g. starting Deluge) and
prints both the raw per-core load% and the per-node oscillator signal
(tracking_err, pruned/not) side by side, so a real single-core spike's
signature is visible directly, without needing to know its timing or
duration in advance.

Uses order_parameter_pruned() -- built earlier this session specifically
to separate an individual node that's fallen out of coherence from the
rest of the (still-calm) ensemble, without touching that node's own
forcing. A single core pegged by Deluge while the other 7 stay idle is
exactly the "one outlier among many calm nodes" case that function was
designed for -- this is the first real-world (not synthetic) test of it.
"""
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter_pruned,
                                node_tracking_err)

REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0


def measure_baseline_target(telem, duration_s=TARGET_MEASURE_S):
    telem.sample_all()
    readings = []
    for _ in range(max(1, int(duration_s / REPORT_INTERVAL_S))):
        time.sleep(REPORT_INTERVAL_S)
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        readings.append(sum(loads) / len(loads) if loads else 0.0)
    return sum(readings) / len(readings)


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else None
    telem = CpuTelemetry()

    print(f"[deluge_watch] {telem.n_cores} cores  measuring real baseline "
          f"for {TARGET_MEASURE_S:.0f}s...", flush=True)
    target_load_frac = measure_baseline_target(telem)
    print(f"[deluge_watch] target_load_frac calibrated: {target_load_frac*100:.1f}%  "
          f"-- start Deluge (or anything else) whenever, no scheduling needed", flush=True)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]

    tick = 0
    start = time.monotonic()
    while duration_s is None or time.monotonic() - start < duration_s:
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains = [gain_from_deviation(l, target_load_frac) for l in loads]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi, coherent = order_parameter_pruned(nodes)
        pruned = [n.index for n in nodes if n not in coherent]

        # Per-core: raw load% next to that core's own oscillator
        # tracking_err, so a real single-core spike's signature in each
        # is directly comparable, live.
        per_core = "  ".join(
            f"c{i}:{loads[i]*100:5.1f}%/e{abs(node_tracking_err(n)):.2f}"
            f"{'*' if n.index in pruned else ' '}"
            for i, n in enumerate(nodes)
        )

        elapsed = time.monotonic() - start
        print(f"[{elapsed:7.1f}s] r={r:.3f} psi={psi:+.3f} pruned={pruned}  {per_core}",
              flush=True)

        tick += 1
        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
