"""sweep_convergence.py — convergence-time-vs-deviation sweep, CPU version.

Same as gpu_layer0/sweep_convergence.py, target fraction changed to
match run_pi2.py's TARGET_LOAD_FRAC (0.50, not gpu_layer0's 0.70).
Pure oscillator math, no /proc dependency -- deviation is driven
directly, so this runs anywhere, not just on Pi2.
"""
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)

N_NODES = 4                # matches Pi2's real core count
TARGET_LOAD_FRAC = 0.50    # matches run_pi2.py
INTERVAL_S = 0.5
MAX_INTERVALS = 40         # 20s ceiling per deviation level
LOCK_THRESHOLD = 0.99
LOCK_SUSTAIN = 3           # must stay >= threshold for this many consecutive intervals

DEVIATIONS = [0.0, 0.02, 0.05, 0.10, 0.20, 0.30, 0.50]  # capped at 0.50: load_frac in [0,1], target=0.50


def run_one(deviation, seed_base=0):
    nodes = [Layer0Node(i + seed_base) for i in range(N_NODES)]
    gain = gain_from_deviation(TARGET_LOAD_FRAC + deviation, TARGET_LOAD_FRAC)
    r_trace = []
    locked_at = None
    sustain_count = 0
    for i in range(MAX_INTERVALS):
        integrate_interval(nodes, [gain] * N_NODES, INTERVAL_S)
        r, _psi = order_parameter(nodes)
        r_trace.append(r)
        if r >= LOCK_THRESHOLD:
            sustain_count += 1
            if sustain_count >= LOCK_SUSTAIN and locked_at is None:
                locked_at = i - LOCK_SUSTAIN + 1
        else:
            sustain_count = 0
    return gain, r_trace, locked_at


def main():
    print(f"{'deviation':>10} {'gain':>8} {'locked_at_interval':>20} {'locked_at_s':>12} {'final_r':>9}")
    for dev in DEVIATIONS:
        gain, r_trace, locked_at = run_one(dev)
        locked_s = f"{locked_at * INTERVAL_S:.1f}" if locked_at is not None else "never"
        locked_str = str(locked_at) if locked_at is not None else "never"
        print(f"{dev:>10.2f} {gain:>8.1f} {locked_str:>20} {locked_s:>12} {r_trace[-1]:>9.4f}")

    print()
    print("deviation=0.0 trace (first 10 intervals, expect free-running r, NOT converging to 1):")
    _, r_trace, _ = run_one(0.0)
    print("  " + " ".join(f"{r:.3f}" for r in r_trace[:10]))


if __name__ == "__main__":
    main()
