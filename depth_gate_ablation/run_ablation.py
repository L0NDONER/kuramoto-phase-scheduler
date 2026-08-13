"""run_ablation.py — one condition of the trust-gate vs cascade-depth
ablation. Reuses load_probe's exact spike-injection/measurement
methodology (same schedule, same dynamically-calibrated target, same
z-score analysis) -- only what changes between runs is DEPTH (how many
levels the CPU signal passes through: Layer 0 alone, Layer 0->1, or
Layer 0->1->2) and GATE (whether Layer 1/2's gain is the real
gain_from_meta_state() trust product, a FIXED constant with no
coherence/anchoring/stability modulation at all, or RANDOM -- gain drawn
independently each tick from the same mean/std the trust-gated version
actually produces, but with no dependence on real coherence at all).

Layer 0 always uses its own gain_from_deviation() unchanged in every
condition -- that's not the "trust gate" under test (gain_from_meta_state
only exists at Layer 1+), so ablating it wouldn't isolate anything; it's
the shared, untouched input every condition receives.

FIXED and RANDOM gain are both calibrated (not guessed) from a quick
pre-measurement of what gain_from_meta_state() actually produces during
real calm baseline conditions. FIXED alone left a real confound
unresolved: gain_from_meta_state()'s TIME-VARYING nature and its
INFORMED nature (reacting to real coherence) are two different
properties, and a fixed-vs-trust comparison can't tell which one
mattered -- FIXED ablates both at once. RANDOM ablates only the informed
part, keeping the same variability (matched mean AND std, not just
mean) but with no connection to what's actually happening upstream. If
RANDOM scores near FIXED's z~=0, that confirms it's the informed judgment
that matters, not mere variability. If RANDOM scores near TRUST's
real-but-modest z, that would mean variability alone was doing the work
and "trust-gated" oversold the mechanism.

Usage: python3 run_ablation.py <depth 1|2|3> <gate trust|fixed|random> [out_csv]
"""
import csv
import math
import multiprocessing
import random
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval as integrate_interval_0,
                                order_parameter)
from layer1_oscillator import (Layer1Node, K1_MAX as K_MAX_CEILING,
                                gain_from_meta_state as gain_from_meta_state_1,
                                integrate_interval as integrate_interval_1)
from layer2_oscillator import (Layer2Node,
                                gain_from_meta_state as gain_from_meta_state_2,
                                integrate_interval as integrate_interval_2)
from load_spike import spike

REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0
BASELINE_S = 30.0
SPIKE_DURATION_S = 15.0
COOLDOWN_S = 30.0
TOTAL_DURATION_S = BASELINE_S + SPIKE_DURATION_S + COOLDOWN_S


def measure_baseline_target(telem, duration_s=TARGET_MEASURE_S):
    telem.sample_all()
    readings = []
    for _ in range(max(1, int(duration_s / REPORT_INTERVAL_S))):
        time.sleep(REPORT_INTERVAL_S)
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        readings.append(sum(loads) / len(loads) if loads else 0.0)
    return sum(readings) / len(readings)


def measure_gain1_stats(telem, nodes, target_load_frac, duration_s=TARGET_MEASURE_S):
    """Runs Layer 0 + real trust-gated Layer 1 for duration_s under calm
    conditions and returns (mean, std) of the real gain1 it actually
    produced -- FIXED uses just the mean; RANDOM uses both, so its
    sampled gain has matched variability, not just matched average."""
    layer1_node = Layer1Node()
    prev_abs_err1 = 0.0
    delta_abs_err1 = 0.0
    gains_seen = []
    for _ in range(max(1, int(duration_s / REPORT_INTERVAL_S))):
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains0 = [gain_from_deviation(l, target_load_frac) for l in loads]
        integrate_interval_0(nodes, gains0, REPORT_INTERVAL_S)
        r0, psi0 = order_parameter(nodes)
        gain1 = gain_from_meta_state_1(r0, abs(psi0), delta_abs_err1)
        integrate_interval_1(layer1_node, gain1, psi0, REPORT_INTERVAL_S)
        tracking_err1 = ((layer1_node.theta - psi0 + math.pi) % (2 * math.pi)) - math.pi
        abs_err1 = abs(tracking_err1)
        delta_abs_err1 = abs_err1 - prev_abs_err1
        prev_abs_err1 = abs_err1
        gains_seen.append(gain1)
        time.sleep(REPORT_INTERVAL_S)
    mean = sum(gains_seen) / len(gains_seen)
    var = sum((g - mean) ** 2 for g in gains_seen) / len(gains_seen)
    return mean, var ** 0.5


def random_gain(mean, std):
    """One independent draw, no dependence on real coherence/anchoring/
    stability at all -- clipped to a valid gain range [0, K_MAX], same
    ceiling gain_from_meta_state() itself respects."""
    return max(0.0, min(K_MAX_CEILING, random.gauss(mean, std)))


def run(depth, gate, out_csv):
    assert depth in (1, 2, 3)
    assert gate in ("trust", "fixed", "random")

    telem = CpuTelemetry()
    n_spike_workers = max(1, telem.n_cores // 2)

    print(f"[ablation] depth={depth} gate={gate}  {telem.n_cores} cores, "
          f"spiking {n_spike_workers}", flush=True)

    target_load_frac = measure_baseline_target(telem)
    print(f"[ablation] target_load_frac calibrated: {target_load_frac*100:.1f}%", flush=True)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]

    fixed_gain1 = fixed_gain2 = None
    random_mean = random_std = None
    if gate in ("fixed", "random") and depth >= 2:
        print(f"[ablation] calibrating {gate} gain1 stats from real trust-gated baseline...",
              flush=True)
        mean1, std1 = measure_gain1_stats(telem, nodes, target_load_frac)
        print(f"[ablation] gain1 baseline: mean={mean1:.1f} std={std1:.1f}", flush=True)
        if gate == "fixed":
            fixed_gain1 = mean1
        else:
            random_mean, random_std = mean1, std1
        # Reset nodes -- the calibration run above already moved their
        # phase state, start the real measurement window fresh.
        nodes = [Layer0Node(i) for i in range(telem.n_cores)]
    if gate in ("fixed", "random") and depth >= 3:
        # Layer 2 reuses the same calibrated Layer 1 stats -- both gates
        # share the same K_MAX scale and the same role (gate this
        # level's own forcing), so one calibration is the fair stand-in
        # for both, not a second independently-guessed/measured number.
        fixed_gain2 = fixed_gain1

    layer1_node = Layer1Node() if depth >= 2 else None
    layer2_node = Layer2Node() if depth >= 3 else None
    prev_abs_err1 = delta_abs_err1 = 0.0
    prev_abs_err2 = delta_abs_err2 = 0.0

    spike_proc = None
    spike_started = False
    rows = []

    n_intervals = int(TOTAL_DURATION_S / REPORT_INTERVAL_S)
    for tick in range(n_intervals):
        elapsed = tick * REPORT_INTERVAL_S

        if not spike_started and elapsed >= BASELINE_S:
            spike_started = True
            spike_proc = multiprocessing.Process(
                target=spike, args=(SPIKE_DURATION_S, n_spike_workers))
            spike_proc.start()
            print(f"[ablation] >>> SPIKE at t={elapsed:.1f}s <<<", flush=True)

        spike_active = spike_started and elapsed < BASELINE_S + SPIKE_DURATION_S

        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains0 = [gain_from_deviation(l, target_load_frac) for l in loads]
        integrate_interval_0(nodes, gains0, REPORT_INTERVAL_S)
        r0, psi0 = order_parameter(nodes)

        output_val = psi0   # what depth=1 reports as its own "final signal"
        output_field = "psi0"

        if depth >= 2:
            if gate == "fixed":
                gain1 = fixed_gain1
            elif gate == "random":
                gain1 = random_gain(random_mean, random_std)
            else:
                gain1 = gain_from_meta_state_1(r0, abs(psi0), delta_abs_err1)
            integrate_interval_1(layer1_node, gain1, psi0, REPORT_INTERVAL_S)
            tracking_err1 = ((layer1_node.theta - psi0 + math.pi) % (2 * math.pi)) - math.pi
            abs_err1 = abs(tracking_err1)
            delta_abs_err1 = abs_err1 - prev_abs_err1
            prev_abs_err1 = abs_err1
            output_val = tracking_err1
            output_field = "tracking_err1"

        if depth >= 3:
            r1_for_l2 = 1.0  # single in-process Layer1 source, trivially self-coherent
            if gate == "fixed":
                gain2 = fixed_gain2
            elif gate == "random":
                gain2 = random_gain(random_mean, random_std)
            else:
                gain2 = gain_from_meta_state_2(r1_for_l2, abs(layer1_node.theta), delta_abs_err2)
            integrate_interval_2(layer2_node, gain2, layer1_node.theta, REPORT_INTERVAL_S)
            tracking_err2 = ((layer2_node.theta - layer1_node.theta + math.pi)
                              % (2 * math.pi)) - math.pi
            abs_err2 = abs(tracking_err2)
            delta_abs_err2 = abs_err2 - prev_abs_err2
            prev_abs_err2 = abs_err2
            output_val = tracking_err2
            output_field = "tracking_err2"

        avg_load = sum(loads) / len(loads) if loads else 0.0
        rows.append(dict(elapsed_s=elapsed, avg_load_frac=avg_load,
                          output_field=output_field, output_val=output_val,
                          spike_active=int(spike_active)))

        time.sleep(REPORT_INTERVAL_S)

    if spike_proc is not None:
        spike_proc.join()

    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[ablation] wrote {len(rows)} rows to {out_csv}", flush=True)


if __name__ == "__main__":
    depth = int(sys.argv[1])
    gate = sys.argv[2]
    out_csv = sys.argv[3] if len(sys.argv) > 3 else f"result_d{depth}_{gate}.csv"
    run(depth, gate, out_csv)
