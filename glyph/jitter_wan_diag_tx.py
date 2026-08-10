#!/usr/bin/env python3
"""
glyph/jitter_wan_diag_tx.py — sends an alternating 0/1 tick stream (flat
and jittered ticks side by side) at the real cadence/duration
jitter_wan_tx.py uses, so the receiver can log the RAW inter-arrival
interval distribution over the real WAN path under sustained load,
instead of guessing thresholds from a spaced-out ping.

Usage: python3 glyph/jitter_wan_diag_tx.py [n_ticks]
"""
import random, socket, struct, sys, time

EC2_WG_IP = "10.8.0.1"
PORT      = 7411
MAGIC     = 0x4A44  # "JD"
TICK_S    = 0.01
TICKS_PER_BIT = 24
JITTER_SIGMA  = 0.035

n_bits = int(sys.argv[1]) if len(sys.argv) > 1 else 14   # 14 bits -> 7 bit-pairs of 0,1
bits = [(i % 2) for i in range(n_bits)]   # alternating 0,1,0,1,...
print(f"[diag-tx] sending known pattern: {bits}", flush=True)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rng = random.Random()
next_at = time.time() + 1.0
seq = 0
for bit in bits:
    for _ in range(TICKS_PER_BIT):
        jitter = rng.gauss(0, JITTER_SIGMA) if bit else 0.0
        fire_at = next_at + jitter
        remaining = fire_at - time.time()
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        while time.time() < fire_at:
            pass
        sock.sendto(struct.pack(">HI", MAGIC, seq), (EC2_WG_IP, PORT))
        seq += 1
        next_at += TICK_S
print(f"[diag-tx] sent {seq} ticks, pattern {bits}", flush=True)
