"""sustained_deviation_watch.py — a real consumer of the substrate's own
(gain, tracking_err) output, catching the failure mode watch_deluge.py's
live log exposed: a core sustained far above target gets tightly LOCKED
(small tracking_err), not flagged, by order_parameter_pruned() --
because gain_from_deviation() rewards a large, sustained deviation with
a large gain, and large gain means a tighter lock (same
sin(err)~=omega/gain relationship from earlier this session). Small
tracking_err alone can't tell "genuinely near target" apart from
"tightly locked onto a real, ongoing deviation" -- this consumer looks
at gain and tracking_err TOGETHER, per core, without touching
layer0_oscillator.py's actual math at all.

Two real fixes made after the first live run against actual Deluge
traffic didn't fire (confirmed live 2026-08-13):

1. HIGH_GAIN_THRESHOLD lowered from 0.5*K_MAX (4000) to 0.25*K_MAX
   (2000) -- the first threshold was data-blind (an arbitrary fraction
   of K_MAX); this one is informed by what a real, clearly-loaded core
   actually produced that day (observed gain ~2280-3240 while c7 sat at
   68-86% against an elevated ~32%-baseline target) -- 2000 sits
   comfortably below that real range instead of just above it.

2. Sustain tracking moved from per-fixed-core-index to "is ANY core
   currently meeting the condition" -- mpstat during that same live run
   showed the hot load wasn't pinned to one core, it was real (genuinely
   sustained, minute after minute) but kept moving between cores, very
   likely IRQ/softirq balancing spreading Deluge's network-interrupt
   load around. A per-core-index sustain counter systematically misses
   that: no single index ever accumulates enough consecutive hits even
   though the aggregate condition ("some core is under real deviation
   right now") was true continuously. This version accumulates sustain
   on the aggregate condition and reports whichever core(s) currently
   qualify each time it fires.

TIGHT_LOCK_MAX = pi/12: reuses psi_bands.py's "anchored" boundary, the
same tight-lock convention already established elsewhere in this
codebase, not a new guessed number -- unchanged from the first version.

Sustained, not single-tick: same hysteresis discipline as
reflex_power_cap.py's SUSTAIN_CHECKS and regime_classifier.py's
hysteresis band, just applied to the aggregate condition now instead of
a single core's.

Third fix, this version: cpu_telemetry.py now breaks busy time down by
category (usr/sys/irq/soft), so this reports WHY a flagged core is busy
-- "network" (irq+soft dominant, real interrupt-processing load from
download traffic, confirmed live via mpstat) vs "process" (usr+sys
dominant, real application CPU work, confirmed live via `ps` showing
deluge-gtk's own %CPU) -- instead of requiring a human to run mpstat by
hand each time, which is literally how this distinction got made the
first time.

Pure detection + logging. No actuation.
"""
import math
import sys
import time

from cpu_telemetry import CpuTelemetry
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, node_tracking_err, K_MAX)


def dominant_cause(sample):
    """process (usr+sys) vs network (irq+soft) -- whichever share of this
    core's busy time is larger, labeled with its own percentage of the
    total interval (not a percentage of just the busy time), so 'network
    62%' means 62% of the whole interval was interrupt processing."""
    process_frac = sample.usr_frac + sample.sys_frac
    network_frac = sample.irq_frac + sample.soft_frac
    if network_frac > process_frac:
        return f"network {network_frac*100:.0f}%"
    return f"process {process_frac*100:.0f}%"

REPORT_INTERVAL_S = 0.5
TARGET_MEASURE_S = 5.0
HIGH_GAIN_THRESHOLD = 0.25 * K_MAX
TIGHT_LOCK_MAX = math.pi / 12
SUSTAIN_TICKS = 10   # 5s at REPORT_INTERVAL_S=0.5


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

    print(f"[sustained_dev] {telem.n_cores} cores  measuring real baseline "
          f"for {TARGET_MEASURE_S:.0f}s...", flush=True)
    target_load_frac = measure_baseline_target(telem)
    print(f"[sustained_dev] target={target_load_frac*100:.1f}%  "
          f"gain_threshold={HIGH_GAIN_THRESHOLD:.0f}  "
          f"tight_lock_max={TIGHT_LOCK_MAX:.3f}rad  sustain={SUSTAIN_TICKS} ticks", flush=True)

    nodes = [Layer0Node(i) for i in range(telem.n_cores)]
    hit_count = 0     # consecutive ticks with AT LEAST ONE core meeting both conditions
    clear_count = 0   # consecutive ticks with NO core meeting them
    flagged = False

    start = time.monotonic()
    while duration_s is None or time.monotonic() - start < duration_s:
        samples = telem.sample_all()
        loads = [s.load_frac if s else 0.0 for s in samples]
        gains = [gain_from_deviation(l, target_load_frac) for l in loads]

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)

        elapsed = time.monotonic() - start
        qualifying = [
            (i, loads[i], gains[i], abs(node_tracking_err(node)), dominant_cause(samples[i]))
            for i, node in enumerate(nodes)
            if samples[i] is not None
            and gains[i] >= HIGH_GAIN_THRESHOLD and abs(node_tracking_err(node)) < TIGHT_LOCK_MAX
        ]

        if qualifying:
            hit_count += 1
            clear_count = 0
        else:
            clear_count += 1
            hit_count = 0

        if not flagged and hit_count >= SUSTAIN_TICKS:
            flagged = True
            detail = ", ".join(f"c{i}:{l*100:.1f}%/gain={g:.0f}/err={e:.3f}/{c}"
                                for i, l, g, e, c in qualifying)
            print(f"[{elapsed:7.1f}s] >>> SUSTAINED DEVIATION ({detail}) <<<", flush=True)
        elif flagged and clear_count >= SUSTAIN_TICKS:
            flagged = False
            print(f"[{elapsed:7.1f}s] cleared -- no core meeting the condition", flush=True)
        elif flagged and qualifying:
            # Still flagged, but print which core(s) currently qualify --
            # the whole point of the aggregate-not-per-index fix is that
            # this can legitimately change tick to tick.
            detail = ", ".join(f"c{i}:{l*100:.1f}%/gain={g:.0f}/err={e:.3f}/{c}"
                                for i, l, g, e, c in qualifying)
            print(f"[{elapsed:7.1f}s]     still flagged ({detail})", flush=True)

        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
