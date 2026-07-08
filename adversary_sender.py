#!/usr/bin/env python3
"""
adversary_sender.py — deliberately fake a pulse-coded hash train.

No real crystal, no live AxisPulse. This is a plain sleep loop that
targets a chosen per-tick jitter (as a fraction of tick_s) by adding
Gaussian noise to each sleep — i.e. an attacker who has read
pulse_hash_recv.py's verify_jitter() and knows the plausibility band
[MIN_JITTER_FRAC, MAX_JITTER_FRAC] and is trying to land inside it
on purpose, rather than accidentally being too clean or too noisy.

Same wire format as pulse_hash_send.py, so the receiver can't tell
these packets apart from a real sender by content alone — only the
jitter statistics can catch it.

Usage:
    python3 adversary_sender.py --dest HOST --tick-s 0.39 --jitter-frac 0.01
"""
import argparse
import hashlib
import random
import socket
import struct
import time

from axis_pulse import hash_to_bits

PS_MAGIC = 0x5053
PS_START, PS_PULSE, PS_END = 1, 2, 3
PS_START_FMT = ">HBHI"
PS_PULSE_FMT = ">HBH"
PS_END_FMT = ">HB"
PS_PORT = 7470


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="127.0.0.1")
    ap.add_argument("--tick-s", type=float, default=0.39, help="claimed tick period")
    ap.add_argument("--jitter-frac", type=float, default=0.01,
                     help="target per-tick jitter as fraction of tick_s "
                          "(0 = perfectly metronomic, tune to land inside "
                          "[MIN_JITTER_FRAC, MAX_JITTER_FRAC]=[0.0005,0.30])")
    ap.add_argument("--salt", default="adversary")
    args = ap.parse_args()

    tick_s = args.tick_s
    jitter_s = args.jitter_frac * tick_s
    tick_us = int(tick_s * 1e6)

    H = hashlib.sha256(args.salt.encode()).digest()
    bits = hash_to_bits(H)
    n_bits = len(bits)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    print(f"[adversary] no crystal, no AxisPulse — pure sleep loop. "
          f"tick_s={tick_s*1000:.1f}ms target_jitter={jitter_s*1e6:.1f}us "
          f"({args.jitter_frac*100:.3f}% of tick) hash={H.hex()[:16]}...", flush=True)

    start_pkt = struct.pack(PS_START_FMT, PS_MAGIC, PS_START, n_bits, tick_us)
    out.sendto(start_pkt, (args.dest, PS_PORT))

    # ideal (unjittered) tick grid, exactly like a real sender's t_next
    # accumulator — then we deliberately smear each send by gaussian noise
    # sized to land in the target band, instead of letting real network/
    # scheduler jitter determine it. This is the crux: a real sender's
    # jitter is a byproduct it can't tune; here we choose it.
    t0 = time.monotonic()
    n_pulses = 0
    for i, b in enumerate(bits):
        ideal = t0 + (i + 1) * tick_s
        noisy_target = ideal + random.gauss(0, jitter_s)
        gap = noisy_target - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        if b:
            out.sendto(struct.pack(PS_PULSE_FMT, PS_MAGIC, PS_PULSE, i), (args.dest, PS_PORT))
            n_pulses += 1

    ideal_end = t0 + (n_bits + 1) * tick_s
    gap = ideal_end - time.monotonic()
    if gap > 0:
        time.sleep(gap)
    out.sendto(struct.pack(PS_END_FMT, PS_MAGIC, PS_END), (args.dest, PS_PORT))
    print(f"[adversary] END  {n_pulses}/{n_bits} pulsed  hash={H.hex()}", flush=True)


if __name__ == "__main__":
    main()
