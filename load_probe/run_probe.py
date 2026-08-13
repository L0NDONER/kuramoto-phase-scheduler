"""run_probe.py — controlled experiment: does tracking_err1 (Layer 1's
residual against Layer 0's real CPU-load mean-field) detect an injected,
precisely-timed synthetic load spike more sensitively than the raw
average load% itself?

This is the first ground-truth-controlled test of the finding validated
on real market data this session (tracking_err1 correlated -0.87 with
gain, CV amplified ~2x relative to gain's own variation) -- market data
proved the residual carries real signal information, but "real
information" there could only be judged against another derived
quantity (gain), not an independently-known ground truth. Here the
event (a real CPU load spike) is injected by this script at a precisely
logged time, so "did the signal detect it, and how fast" is directly
checkable, not inferred.

Two-level stack, single process, no network (doesn't need
layer0_report.py's wire format -- everything runs in one process here):
  Level 0: real per-core CPU load (cpu_telemetry.py) -> Layer0Node
           ensemble -> (r0, psi0), Layer 0's own mean-field, exactly the
           role market_layer0's (r, psi) played for layer1/run_layer1.py.
  Level 1: Layer1Node RK4-tracks psi0 as a moving target, gain gated by
           gain_from_meta_state(r0, |psi0|, delta_tracking_err1) -- same
           argument mapping run_layer1.py uses (r1=upstream coherence,
           tracking_err0=upstream's own anchoring), just Layer 0 is a
           single in-process source here instead of a multicast peer.

Logs every tick to probe_results.csv: elapsed_s, avg_load_frac (ground
truth raw signal), r0, psi0, tracking_err1, gain1, spike_active (ground
truth: is the injected spike currently running).

Only spikes HALF the machine's cores (not all of them) -- a real,
deliberate load increase, but leaves this session and the rest of the
machine usable while the probe runs instead of pegging everything.
"""
import csv
import math
import multiprocessing
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval as integrate_interval_0,
                                order_parameter)
from layer1_oscillator import (Layer1Node, gain_from_meta_state,
                                integrate_interval as integrate_interval_1)
from load_spike import spike

REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0  # pre-measurement window to calibrate TARGET_LOAD_FRAC to
                         # the machine's REAL current baseline, not a fixed guess --
                         # see the first run's actual bug: TARGET_LOAD_FRAC=0.5 sat
                         # almost equidistant from both baseline (~30%) and spike
                         # (~70%) load, so |load-target| barely moved even though
                         # raw load clearly did. Confirmed live 2026-08-13: baseline
                         # mean|load-0.5|=0.221, spike mean|load-0.5|=0.207 -- the
                         # driving deviation signal never actually saw an event.
BASELINE_S = 30.0      # calm period before the injected spike (lets both levels lock first)
SPIKE_DURATION_S = 15.0
COOLDOWN_S = 30.0      # calm period after the spike, for a symmetric before/after comparison
TOTAL_DURATION_S = BASELINE_S + SPIKE_DURATION_S + COOLDOWN_S

CSV_PATH = "probe_results.csv"


def measure_baseline_target(telem, duration_s=TARGET_MEASURE_S):
    """Samples real load for duration_s and returns its mean -- used as
    TARGET_LOAD_FRAC so the injected spike is a genuine one-directional
    deviation from wherever this machine actually sits right now, not
    from an arbitrary fixed guess."""
    telem.sample_all()
    readings = []
    n_ticks = max(1, int(duration_s / REPORT_INTERVAL_S))
    for _ in range(n_ticks):
        time.sleep(REPORT_INTERVAL_S)
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        readings.append(sum(loads) / len(loads) if loads else 0.0)
    return sum(readings) / len(readings)


def main():
    telem = CpuTelemetry()
    n_spike_workers = max(1, telem.n_cores // 2)

    print(f"[load_probe] {telem.n_cores} cores, spiking {n_spike_workers} of them  "
          f"measuring real baseline load for {TARGET_MEASURE_S:.0f}s...", flush=True)
    target_load_frac = measure_baseline_target(telem)
    print(f"[load_probe] TARGET_LOAD_FRAC calibrated to real baseline: "
          f"{target_load_frac*100:.1f}%", flush=True)

    print(f"[load_probe] schedule: {BASELINE_S:.0f}s baseline -> "
          f"{SPIKE_DURATION_S:.0f}s spike -> {COOLDOWN_S:.0f}s cooldown  "
          f"(total {TOTAL_DURATION_S:.0f}s)", flush=True)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]
    layer1_node = Layer1Node()
    prev_abs_err1 = 0.0
    delta_abs_err1 = 0.0

    spike_proc = None
    spike_started = False
    rows = []

    n_intervals = int(TOTAL_DURATION_S / REPORT_INTERVAL_S)
    for tick in range(n_intervals):
        elapsed = tick * REPORT_INTERVAL_S

        # Trigger the spike exactly once, at the scheduled time -- a
        # detached background process so it doesn't block this loop's
        # own timing.
        if not spike_started and elapsed >= BASELINE_S:
            spike_started = True
            spike_proc = multiprocessing.Process(
                target=spike, args=(SPIKE_DURATION_S, n_spike_workers))
            spike_proc.start()
            print(f"[load_probe] >>> SPIKE INJECTED at t={elapsed:.1f}s <<<", flush=True)

        spike_active = spike_started and elapsed < BASELINE_S + SPIKE_DURATION_S

        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains0 = [gain_from_deviation(l, target_load_frac) for l in loads]

        integrate_interval_0(nodes, gains0, REPORT_INTERVAL_S)
        r0, psi0 = order_parameter(nodes)

        gain1 = gain_from_meta_state(r0, abs(psi0), delta_abs_err1)
        integrate_interval_1(layer1_node, gain1, psi0, REPORT_INTERVAL_S)

        tracking_err1 = ((layer1_node.theta - psi0 + math.pi) % (2 * math.pi)) - math.pi
        abs_err1 = abs(tracking_err1)
        delta_abs_err1 = abs_err1 - prev_abs_err1
        prev_abs_err1 = abs_err1

        avg_load = sum(loads) / len(loads) if loads else 0.0

        rows.append(dict(elapsed_s=elapsed, avg_load_frac=avg_load, r0=r0, psi0=psi0,
                          tracking_err1=tracking_err1, gain1=gain1,
                          spike_active=int(spike_active)))

        print(f"[{elapsed:6.1f}s] load={avg_load*100:5.1f}%  r0={r0:.3f} psi0={psi0:+.3f}  "
              f"tracking_err1={tracking_err1:+.4f}  gain1={gain1:7.1f}  "
              f"{'SPIKE' if spike_active else ''}", flush=True)

        time.sleep(REPORT_INTERVAL_S)

    if spike_proc is not None:
        spike_proc.join()

    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n[load_probe] wrote {len(rows)} rows to {CSV_PATH}", flush=True)


if __name__ == "__main__":
    main()
