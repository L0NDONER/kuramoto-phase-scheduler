#!/usr/bin/env python3
"""
glyph/jitter_encoder.py — sandbox sketch: encode data as tick-jitter,
not phase-velocity like glyph_tx/reader_glyph.c.

Idea: split the oscillator tick stream into fixed-width symbol windows.
Within a window, ticks land exactly on schedule (bit=0) or get a random
jitter added to each tick (bit=1). Decoder measures the stddev of
inter-tick intervals inside each window and thresholds it back to a bit.

This is deliberately NOT wired to reader_glyph.c or the production
AxisPulse pd_dev channel — see conversation: pd_dev is already claimed
as a health signal elsewhere (reflex.py's 2-of-3 vote), so a jitter data
channel needs to live on its own tick stream, not riding on top of it.
Sandbox-only simulation; nothing here touches real hardware.

Usage:
    python3 glyph/jitter_encoder.py "hello world"
"""
import random
import sys

TICK_S       = 0.01   # nominal oscillator period (s), simulated only
TICKS_PER_BIT = 8      # oscillator ticks per symbol window
JITTER_SIGMA  = 0.4 * TICK_S   # stddev added to each tick when bit=1
THRESH        = 0.15 * TICK_S  # decision boundary on measured interval stddev


def text_to_bits(text):
    bits = []
    for byte in text.encode("utf-8"):
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    return bits


def bits_to_text(bits):
    byte_vals = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        byte_vals.append(byte)
    return byte_vals.decode("utf-8", errors="replace")


def encode(bits, seed=None):
    """Return a flat list of tick timestamps, TICKS_PER_BIT per bit."""
    rng = random.Random(seed)
    t = 0.0
    ticks = []
    for bit in bits:
        for _ in range(TICKS_PER_BIT):
            t += TICK_S
            jitter = rng.gauss(0, JITTER_SIGMA) if bit else 0.0
            ticks.append(t + jitter)
    return ticks


def decode(ticks, n_bits):
    """Recover bits by measuring inter-tick interval stddev per window."""
    intervals = [b - a for a, b in zip(ticks, ticks[1:])]
    bits = []
    for i in range(n_bits):
        # only intervals strictly inside this bit's own ticks — the interval
        # spanning into the next bit's first tick would leak its jitter in
        window = intervals[i * TICKS_PER_BIT:i * TICKS_PER_BIT + TICKS_PER_BIT - 1]
        if not window:
            break
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        std = var ** 0.5
        bits.append(1 if std > THRESH else 0)
    return bits


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "hello world"
    bits = text_to_bits(text)

    print(f"TX text : {text!r}")
    print(f"TX bits : {len(bits)} bits, {len(bits)*TICKS_PER_BIT} ticks "
          f"({len(bits)*TICKS_PER_BIT*TICK_S*1000:.0f}ms)")

    ticks = encode(bits)
    rx_bits = decode(ticks, len(bits))
    rx_text = bits_to_text(rx_bits)

    print(f"RX text : {rx_text!r}")
    errors = sum(a != b for a, b in zip(bits, rx_bits))
    print(f"bit errors: {errors}/{len(bits)}")
    print("MATCH" if rx_text == text else "MISMATCH")
