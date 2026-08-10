#!/usr/bin/env python3
"""
glyph/jitter_wan_diag_rx.py — receiver for jitter_wan_diag_tx.py.
Logs raw (seq, arrival_time) and reports the real inter-arrival
interval distribution, out-of-order count, and loss count over the
real WAN path under sustained 100pps load.

Usage: run on EC2.
"""
import socket, struct, time

BIND_IP = "10.8.0.1"
PORT    = 7411
MAGIC   = 0x4A44

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((BIND_IP, PORT))
print(f"[diag-rx] listening on {BIND_IP}:{PORT}", flush=True)

received = []
s.settimeout(30.0)
while True:
    try:
        data, _ = s.recvfrom(64)
    except socket.timeout:
        break
    if len(data) < 6:
        continue
    magic, seq = struct.unpack(">HI", data[:6])
    if magic != MAGIC:
        continue
    received.append((seq, time.time()))
    s.settimeout(1.0)

print(f"[diag-rx] received {len(received)} packets", flush=True)

# out-of-order count: how many arrived with a lower seq than the max seq seen so far
max_seq_so_far = -1
out_of_order = 0
for seq, _ in received:
    if seq < max_seq_so_far:
        out_of_order += 1
    max_seq_so_far = max(max_seq_so_far, seq)
print(f"[diag-rx] out-of-order arrivals: {out_of_order}", flush=True)

seqs = sorted(s for s, _ in received)
if seqs:
    expected = set(range(seqs[0], seqs[-1] + 1))
    missing = expected - set(seqs)
    print(f"[diag-rx] seq range {seqs[0]}..{seqs[-1]}, missing: {len(missing)}", flush=True)

# real inter-arrival intervals in SEND order (sorted by seq)
received.sort(key=lambda r: r[0])
times = [t for _, t in received]
intervals = [b - a for a, b in zip(times, times[1:])]
if intervals:
    mean = sum(intervals) / len(intervals)
    var = sum((x - mean) ** 2 for x in intervals) / len(intervals)
    std = var ** 0.5
    print(f"[diag-rx] intervals (send-order): n={len(intervals)} "
          f"mean={mean*1000:.3f}ms std={std*1000:.3f}ms "
          f"min={min(intervals)*1000:.3f}ms max={max(intervals)*1000:.3f}ms", flush=True)
    # windowed std (matching TICKS_PER_BIT -> TICKS_PER_BIT-1 intervals per
    # window), same statistic decode() actually uses per bit
    TICKS_PER_BIT = 24
    n_windows = len(received) // TICKS_PER_BIT   # matches decode()'s n_bits math exactly
    window_stds = []
    for i in range(n_windows):
        w = intervals[i * TICKS_PER_BIT: i * TICKS_PER_BIT + TICKS_PER_BIT - 1]
        if len(w) < TICKS_PER_BIT - 1:
            continue
        wmean = sum(w) / len(w)
        wvar = sum((x - wmean) ** 2 for x in w) / len(w)
        window_stds.append(wvar ** 0.5)
    if window_stds:
        print(f"[diag-rx] per-window std (n={len(window_stds)}): "
              f"mean={sum(window_stds)/len(window_stds)*1000:.3f}ms "
              f"min={min(window_stds)*1000:.3f}ms max={max(window_stds)*1000:.3f}ms", flush=True)
        print("[diag-rx] each window (ms): " +
              " ".join(f"{s*1000:.2f}" for s in window_stds), flush=True)
