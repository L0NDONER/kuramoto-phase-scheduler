"""run_pi1.py — Layer 0 prototype for Pi1's CPU, one oscillator per core.

Same shape as gpu_layer0/run_kaggle.py: sample real telemetry once per
REPORT_INTERVAL_S, derive a per-core coupling gain from how far that
core's load sits from TARGET_LOAD_FRAC, RK4-integrate, report the order
parameter plus the raw telemetry that drove it.

N=4 here (Pi1's real core count) is a meaningfully better swarm size
than gpu_layer0's N=2 -- still below the ~10 threshold where r becomes
a fully trustworthy coherence statistic, but closer, and multi-core
rather than dual-GPU makes the "many independent oscillators" framing
less of a stretch.

First cpu_telemetry sample has no prior state to diff against (returns
None for every core) -- discarded here rather than treated as real data.

Unlike Pi2, Pi1 already runs power_consensus_pi.py (the 10-arm DVFS
swarm), which actively WRITES scaling_setspeed under the userspace
governor. This module only ever READS /proc/stat and scaling_cur_freq
-- no conflict, it's a passive observer layered on top of an actively-
controlled system, not a second thing fighting for the same actuator.
"""
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)
from layer0_report import mcast_out, send_report

NODE_NAME = "pi1"
TARGET_LOAD_FRAC = 0.50   # stand-in policy: "keep each core around 50% load"
REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0


def main():
    telem = CpuTelemetry()
    print(f"[layer0-cpu] {telem.n_cores} cores", flush=True)

    # discard the first sample -- no prior state to diff against yet
    telem.sample_all()
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

        parts = " ".join(
            f"core{s.core if s else i}: load={l*100:4.1f}% "
            f"freq={s.freq_mhz if s else 0:.0f}MHz temp={s.temp_c if s else 0:.1f}C k={k:5.1f}"
            for i, (s, l, k) in enumerate(zip(samples, loads, gains))
        )
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  {parts}",
              flush=True)

        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
