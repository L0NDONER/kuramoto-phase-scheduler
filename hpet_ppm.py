#!/usr/bin/env python3
"""
hpet_ppm.py — raw 24MHz HPET crystal drift, independent of NTP/adjtimex.

adjtimex freq_ppm reports the kernel's NTP *correction* against whatever
clocksource is active (TSC here) — post-discipline, not the crystal's
honest rate. This instead mmaps /dev/hpet and diffs the free-running
main counter against CLOCK_MONOTONIC_RAW, which the kernel guarantees
NTP never slews. Nominal period comes straight out of the hardware's own
capabilities register (COUNTER_CLK_PERIOD), not an assumed 24.000000MHz.

Requires root (mmap of /dev/hpet).
"""
import mmap
import os
import struct
import time

CAP_REG = 0x00
COUNTER_REG = 0xF0


class Hpet:
    def __init__(self, path="/dev/hpet"):
        self.fd = os.open(path, os.O_RDONLY)
        self.mem = mmap.mmap(self.fd, 0x1000, prot=mmap.PROT_READ)
        caps = struct.unpack_from("<Q", self.mem, CAP_REG)[0]
        self.period_fs = caps >> 32                    # femtoseconds/tick, from hardware
        self.nominal_hz = 1e15 / self.period_fs
        self.counter_bits = 64 if (caps >> 13) & 1 else 32

    def counter(self):
        return struct.unpack_from("<Q", self.mem, COUNTER_REG)[0]

    def close(self):
        self.mem.close()
        os.close(self.fd)


def read_hpet_ppm(hpet, sample_s=1.0, n=5):
    """Median drift (ppm) of the HPET crystal vs CLOCK_MONOTONIC_RAW.

    A single sample_s-long diff is exposed to scheduling jitter on whatever
    thread runs the sleep — one delayed wakeup skews the whole reading (seen
    live: one-off -25ppm spike against a steady ~1ppm baseline). Splitting
    into n sub-samples and taking the median rejects that kind of one-off
    outlier without needing a longer total blocking window.
    """
    sub_s = sample_s / n
    samples = []
    t_prev = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    c_prev = hpet.counter()
    for _ in range(n):
        time.sleep(sub_s)
        t = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
        c = hpet.counter()
        measured_hz = (c - c_prev) / (t - t_prev)
        samples.append((measured_hz - hpet.nominal_hz) / hpet.nominal_hz * 1e6)
        t_prev, c_prev = t, c

    samples.sort()
    ppm = samples[n // 2]
    measured_hz = hpet.nominal_hz * (1 + ppm / 1e6)
    return ppm, measured_hz


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("needs root (mmap of /dev/hpet)")

    h = Hpet()
    print(f"[hpet] nominal_hz={h.nominal_hz:.3f} period_fs={h.period_fs} "
          f"counter_bits={h.counter_bits}", flush=True)
    try:
        while True:
            ppm, hz = read_hpet_ppm(h, sample_s=1.0)
            print(f"[hpet] measured_hz={hz:.3f} ppm={ppm:+.4f}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        h.close()
