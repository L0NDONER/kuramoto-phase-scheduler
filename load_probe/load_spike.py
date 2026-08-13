"""load_spike.py — controlled, precisely-timed synthetic CPU load spike,
for testing whether tracking_err1 detects an injected event more
sensitively than raw load% (see run_probe.py). Real worker processes
doing real CPU-bound work, not a faked telemetry value -- otherwise this
wouldn't test anything about the actual measurement pipeline.
"""
import multiprocessing
import time


def _burn(duration_s):
    """Busy-loop for duration_s wall-clock seconds -- pure CPU-bound
    integer math, no sleeps, no I/O, so it actually saturates a core."""
    end = time.monotonic() + duration_s
    x = 0
    while time.monotonic() < end:
        x = (x * 1103515245 + 12345) & 0x7fffffff


def spike(duration_s, n_workers):
    """Spawns n_workers real processes (not threads -- CPython's GIL
    would stop threads from actually saturating multiple cores) each
    burning one core for duration_s, blocks until they're all done."""
    procs = [multiprocessing.Process(target=_burn, args=(duration_s,))
             for _ in range(n_workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
