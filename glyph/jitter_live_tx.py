#!/usr/bin/env python3
"""
glyph/jitter_live_tx.py — jitter_encoder.py's tick-jitter scheme, sent
as real UDP packets with real timing instead of synthetic timestamps.

Own multicast group (239.0.0.6:7409), not AxisPulse/pd_dev — see
jitter_encoder.py's docstring for why this needs its own tick stream.

Usage:
    python3 glyph/jitter_live_tx.py "hello world"
"""
import random, socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from jitter_encoder import text_to_bits, TICK_S, TICKS_PER_BIT, JITTER_SIGMA

GRP, PORT = "239.0.0.6", 7409
MAGIC = 0x4A54  # "JT"


def sleep_until(t):
    """Sleep close to t, then busy-wait the last ~1ms for sub-ms precision."""
    while True:
        remaining = t - time.time()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        # else: busy-wait the final ~1-2ms


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "hello world"
    bits = text_to_bits(text)
    print(f"[jitter-tx] {text!r} -> {len(bits)} bits, "
          f"{len(bits) * TICKS_PER_BIT} ticks", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    rng = random.Random()
    seq = 0
    next_at = time.time() + 1.0  # let receiver settle before the first tick
    for bit in bits:
        for _ in range(TICKS_PER_BIT):
            jitter = rng.gauss(0, JITTER_SIGMA) if bit else 0.0
            sleep_until(next_at + jitter)
            sock.sendto(struct.pack(">HI", MAGIC, seq), (GRP, PORT))
            seq += 1
            next_at += TICK_S
    print(f"[jitter-tx] done — {seq} ticks sent", flush=True)


if __name__ == "__main__":
    main()
