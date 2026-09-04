"""detector_comparison.py — does the Kuramoto/RK4/gain machinery in
sustained_deviation_watch.py actually detect anything a plain threshold
check wouldn't? Runs both detectors against the exact same real telemetry
samples, same tick, so there's no risk of the two missing different
moments due to sampling jitter between separate processes.

OSCILLATOR detector: identical logic to sustained_deviation_watch.py --
gain_from_deviation() -> Layer0Node RK4 integration -> gain AND
tracking_err both checked.

SIMPLE detector: no oscillator, no RK4, no integration state at all --
just |load - target| compared directly against the SAME equivalent
threshold (HIGH_GAIN_THRESHOLD / K_BASE = 2000/6000 = 0.3333, i.e. the
exact deviation that would have produced gain=2000 in the oscillator
version), same aggregate-any-core sustain-for-10-ticks hysteresis. The
only thing that differs between the two detectors is whether the
decision passes through the oscillator's gain+tracking_err machinery or
just compares the raw number directly -- same threshold, same sustain
window, same telemetry, same tick.
"""
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, node_tracking_err, K_MAX, K_BASE)

REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0
HIGH_GAIN_THRESHOLD = 0.25 * K_MAX
SIMPLE_DEVIATION_THRESHOLD = HIGH_GAIN_THRESHOLD / K_BASE  # exact equivalent, not a separate guess
TIGHT_LOCK_MAX = 3.14159265358979 / 12
SUSTAIN_TICKS = 10


def measure_baseline_target(telem, duration_s=TARGET_MEASURE_S):
    telem.sample_all()
    readings = []
    for _ in range(max(1, int(duration_s / REPORT_INTERVAL_S))):
        time.sleep(REPORT_INTERVAL_S)
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        readings.append(sum(loads) / len(loads) if loads else 0.0)
    return sum(readings) / len(readings)


class SustainTracker:
    """Same aggregate hit/clear/flagged hysteresis both detectors share --
    factored out so the comparison isolates only the decision rule
    (oscillator vs raw threshold), not two independently-reimplemented
    copies of the same sustain logic."""
    def __init__(self, label):
        self.label = label
        self.hit_count = 0
        self.clear_count = 0
        self.flagged = False

    def update(self, qualifying, elapsed):
        if qualifying:
            self.hit_count += 1
            self.clear_count = 0
        else:
            self.clear_count += 1
            self.hit_count = 0

        if not self.flagged and self.hit_count >= SUSTAIN_TICKS:
            self.flagged = True
            detail = ", ".join(f"c{i}:{l*100:.1f}%" for i, l in qualifying)
            print(f"[{elapsed:7.1f}s] [{self.label}] >>> FLAGGED ({detail}) <<<", flush=True)
        elif self.flagged and self.clear_count >= SUSTAIN_TICKS:
            self.flagged = False
            print(f"[{elapsed:7.1f}s] [{self.label}] cleared", flush=True)


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else None
    telem = CpuTelemetry()

    print(f"[compare] {telem.n_cores} cores  measuring real baseline "
          f"for {TARGET_MEASURE_S:.0f}s...", flush=True)
    target_load_frac = measure_baseline_target(telem)
    print(f"[compare] target={target_load_frac*100:.1f}%  "
          f"gain_threshold={HIGH_GAIN_THRESHOLD:.0f}  "
          f"equivalent_deviation_threshold={SIMPLE_DEVIATION_THRESHOLD:.4f}  "
          f"sustain={SUSTAIN_TICKS} ticks", flush=True)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]
    osc_tracker = SustainTracker("OSCILLATOR")
    simple_tracker = SustainTracker("SIMPLE")

    start = time.monotonic()
    while duration_s is None or time.monotonic() - start < duration_s:
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        elapsed = time.monotonic() - start

        # OSCILLATOR path
        gains = [gain_from_deviation(l, target_load_frac) for l in loads]
        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        osc_qualifying = [
            (i, loads[i]) for i, node in enumerate(nodes)
            if samples[i] is not None
            and gains[i] >= HIGH_GAIN_THRESHOLD and abs(node_tracking_err(node)) < TIGHT_LOCK_MAX
        ]
        osc_tracker.update(osc_qualifying, elapsed)

        # SIMPLE path -- no oscillator, no RK4, just the raw number
        simple_qualifying = [
            (i, loads[i]) for i, s in enumerate(samples)
            if s is not None and abs(loads[i] - target_load_frac) >= SIMPLE_DEVIATION_THRESHOLD
        ]
        simple_tracker.update(simple_qualifying, elapsed)

        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
