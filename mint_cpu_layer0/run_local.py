"""run_local_simple.py -- replaces run_local.py's Kuramoto machinery
with a direct mean-absolute-deviation across real per-core load
readings, same simplification as pi2_cpu_layer0/run_pi2_simple.py and
pi1_thermal_layer0/run_pi1_thermal.py tonight (2026-09-03). See
run_pi2_simple.py's docstring for the full reasoning -- identical here,
just this machine's own core count instead of 4.

r/psi still broadcast via the same layer0_report.py wire, same [0,1]
range and "closer to 1 = closer to target" direction, so layer1's
mean-field aggregation (which already consumes mint's r/psi as one of
its 6 real inputs) needs no changes.
"""
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_report import mcast_out, send_report

NODE_NAME = "mint"
# Real calibrated baseline, live sample 2026-09-03 (30s, 400 points
# across 8 cores): mean=0.2890 stdev=0.1210. Higher than pi2's, real --
# this machine was under genuine load from this session's own work at
# sample time, not a clean idle baseline. SCALE set so ~2 stdev of
# deviation drives r toward 0. Single ~30s window, same caveat as
# pi1_thermal's/pi2's own calibration comments -- worth resampling on
# a quieter night if this ever needs to mean something precise.
TARGET_LOAD_FRAC = 0.2890
SCALE = 4.132
REPORT_INTERVAL_S = 0.5
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0


def main():
    telem = CpuTelemetry()
    print(f"[layer0-cpu-{NODE_NAME}] {telem.n_cores} cores", flush=True)

    telem.sample_all()
    time.sleep(REPORT_INTERVAL_S)

    report_sock = mcast_out()

    n_intervals = max(1, int(TOTAL_DURATION_S / REPORT_INTERVAL_S))
    for tick in range(n_intervals):
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        deviations = [abs(l - TARGET_LOAD_FRAC) for l in loads]
        mean_deviation = sum(deviations) / len(deviations) if deviations else 0.0

        r = max(0.0, 1.0 - mean_deviation * SCALE)
        psi = 0.0

        send_report(report_sock, NODE_NAME, r, psi)

        avg_load = sum(loads) / len(loads) if loads else 0.0
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  "
              f"avg_load={avg_load*100:.1f}%  n_cores={telem.n_cores}", flush=True)

        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
