#!/usr/bin/env python3
"""
test_physical_regression.py — permanent regression harness for
verify_physical() (pulse_hash_recv.py), run against three sender
regimes: the real production pacer, a flat-forgery adversary, and a
Nazare-style bursty adversary.

This does NOT assert "all adversaries always fail" — that would be
dishonest about what verify_physical() currently catches. Findings from
the WAN test campaign (2026-07-03, see nazare_vs_flat.png):

  - real quartz pacing (LAN or WAN, EC2 over WireGuard): always PHYSICAL.
  - a flat forger tuned to ~0.1%-2% of tick_s as target jitter: PASSES.
    Not caught. Open problem — flagged, not silently accepted.
  - an obvious nazare burst (few large spikes, e.g. burst_prob=0.03,
    burst_mult=8): shape check (burst_ratio/kurtosis) REJECTS it.
  - a nazare burst tuned small enough / rare enough that spikes land on
    ticks with no pulse (bit=0, ~50% of any train): invisible to any
    check built from pulse-arrival timestamps alone. Not caught. Open
    problem, structural — not a threshold tuning issue.

Run this after any change to verify_jitter/verify_shape/verify_physical
or their thresholds, to see which of these four cases moved.
"""
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, "/home/martin/claude")
from pulse_hash_recv import mcast_in, verify_physical, PS_MAGIC, PS_PORT, PS_START, PS_PULSE, PS_END

DEST = "127.0.0.1"


def run_one(cmd, timeout_s=40):
    """Fire `cmd` (a sender subprocess) and capture the resulting verify_physical() verdict."""
    s = mcast_in("239.0.0.7", PS_PORT)
    s.settimeout(timeout_s)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    n_bits = tick_s = None
    arrivals = []
    try:
        while n_bits is None:
            data, _ = s.recvfrom(64)
            if len(data) < 3:
                continue
            magic, ptype = struct.unpack_from(">HB", data)
            if magic != PS_MAGIC or ptype != PS_START:
                continue
            _, _, n_bits, tick_us = struct.unpack_from(">HBHI", data)
            tick_s = tick_us / 1e6

        while True:
            data, _ = s.recvfrom(64)
            if len(data) < 3:
                continue
            magic, ptype = struct.unpack_from(">HB", data)
            if magic != PS_MAGIC:
                continue
            if ptype == PS_PULSE:
                t_arrival = time.monotonic()
                _, _, idx = struct.unpack_from(">HBH", data)
                if 0 <= idx < n_bits:
                    arrivals.append((idx, t_arrival))
            elif ptype == PS_END:
                break
    finally:
        proc.wait(timeout=5)
        s.close()

    return verify_physical(arrivals, tick_s)


def main():
    cases = [
        ("real (production pacer, --demo salt)",
         ["python3", "/home/martin/claude/pulse_hash_send.py", "--demo", "regtest", "--dest", DEST],
         True),
        ("flat adversary (jitter-frac=0.02, known gap)",
         ["python3", "/home/martin/claude/adversary_sender.py",
          "--dest", DEST, "--tick-s", "0.05", "--jitter-frac", "0.02", "--salt", "regtest_flat"],
         None),  # no assertion — documented open problem
        ("nazare obvious burst (prob=0.03 mult=8)",
         ["python3", "/home/martin/claude/nazare_sender.py",
          "--dest", DEST, "--tick-s", "0.05", "--mode", "quiet-burst",
          "--base-jitter-frac", "0.001", "--burst-prob", "0.03", "--burst-mult", "8",
          "--salt", "regtest_nazare"],
         False),
    ]

    failures = 0
    for name, cmd, expect_physical in cases:
        pv = run_one(cmd)
        got = pv["physical"]
        if expect_physical is None:
            status = f"UNASSERTED (physical={got}, known gap)"
        elif got == expect_physical:
            status = f"OK (physical={got})"
        else:
            status = f"REGRESSION (expected physical={expect_physical}, got {got})"
            failures += 1
        print(f"[{name}] {status}")
        if pv["jitter"]:
            print(f"    jitter: {pv['jitter']['verdict']}")
        if pv["shape"]:
            print(f"    shape:  {pv['shape']['verdict']}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
