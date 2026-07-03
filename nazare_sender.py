#!/usr/bin/env python3
"""
nazare_sender.py — structured/bursty timing adversary, not flat Gaussian.

adversary_sender.py tests "can a scripted sender fake a *flat* jitter
profile and land inside the plausibility band." This tests something
verify_jitter has no model for at all: *shape*. It reduces every train
to one number (stdev of adjacent-tick residual diffs) and thresholds
that. It has no kurtosis/tail check, no run-length check, nothing that
would catch a non-stationary process.

Two regimes, same wire format as pulse_hash_send.py:

  --mode quiet-burst : background jitter tight and near-zero (would read
      TOO CLEAN alone), punctuated by rare large delay spikes ("Nazare
      waves"). Tests whether a *mostly metronomic* signal with occasional
      huge surges can still average into the PLAUSIBLE band, or whether
      one real network congestion event is enough to flip a genuine
      sender to TOO NOISY (false reject of real physical jitter).

  --mode ramp : jitter that ramps up, peaks, and decays across the train
      (a genuine swell shape) instead of being drawn i.i.d. per tick.
      Tests whether the stdev statistic is blind to serial structure
      that a human looking at the arrival trace would immediately see.

Usage:
    python3 nazare_sender.py --dest HOST --mode quiet-burst \
        --burst-prob 0.05 --burst-mult 8 --base-jitter-frac 0.001
"""
import argparse
import hashlib
import math
import random
import socket
import struct
import time

PS_MAGIC = 0x5053
PS_START, PS_PULSE, PS_END = 1, 2, 3
PS_START_FMT = ">HBHI"
PS_PULSE_FMT = ">HBH"
PS_END_FMT = ">HB"
PS_PORT = 7470


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="127.0.0.1")
    ap.add_argument("--tick-s", type=float, default=0.05)
    ap.add_argument("--mode", choices=["quiet-burst", "ramp"], default="quiet-burst")
    ap.add_argument("--base-jitter-frac", type=float, default=0.001)
    ap.add_argument("--burst-prob", type=float, default=0.05)
    ap.add_argument("--burst-mult", type=float, default=8.0,
                     help="burst delay size, as a multiple of tick_s")
    ap.add_argument("--ramp-peak-frac", type=float, default=0.10,
                     help="ramp mode: peak jitter as fraction of tick_s")
    ap.add_argument("--salt", default="nazare")
    args = ap.parse_args()

    tick_s = args.tick_s
    tick_us = int(tick_s * 1e6)
    base_jitter_s = args.base_jitter_frac * tick_s
    burst_delay_s = args.burst_mult * tick_s

    H = hashlib.sha256(args.salt.encode()).digest()
    bits = []
    for byte in H:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    n_bits = len(bits)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    print(f"[nazare] mode={args.mode} tick_s={tick_s*1000:.1f}ms "
          f"base_jitter={base_jitter_s*1e6:.1f}us "
          f"{'burst_prob=' + str(args.burst_prob) + ' burst=' + str(burst_delay_s*1e3) + 'ms' if args.mode=='quiet-burst' else 'ramp_peak=' + str(args.ramp_peak_frac)}",
          flush=True)

    start_pkt = struct.pack(PS_START_FMT, PS_MAGIC, PS_START, n_bits, tick_us)
    out.sendto(start_pkt, (args.dest, PS_PORT))

    t0 = time.monotonic()
    n_pulses = 0
    n_bursts = 0
    cum_extra = 0.0  # bursts/ramp push wall-clock later; track accumulated offset

    for i, b in enumerate(bits):
        ideal = t0 + (i + 1) * tick_s

        if args.mode == "quiet-burst":
            noise = random.gauss(0, base_jitter_s)
            if random.random() < args.burst_prob:
                noise += burst_delay_s
                n_bursts += 1
        else:  # ramp: triangular envelope over the whole train, 0 -> peak -> 0
            phase = i / max(1, n_bits - 1)
            envelope = 1 - abs(2 * phase - 1)  # 0..1..0
            local_jitter_s = args.ramp_peak_frac * tick_s * envelope
            noise = random.gauss(0, max(local_jitter_s, base_jitter_s))

        cum_extra += noise
        target = ideal + cum_extra
        gap = target - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        if b:
            out.sendto(struct.pack(PS_PULSE_FMT, PS_MAGIC, PS_PULSE, i), (args.dest, PS_PORT))
            n_pulses += 1

    gap = (t0 + (n_bits + 1) * tick_s + cum_extra) - time.monotonic()
    if gap > 0:
        time.sleep(gap)
    out.sendto(struct.pack(PS_END_FMT, PS_MAGIC, PS_END), (args.dest, PS_PORT))
    print(f"[nazare] END  {n_pulses}/{n_bits} pulsed  bursts={n_bursts}  hash={H.hex()[:16]}...", flush=True)


if __name__ == "__main__":
    main()
