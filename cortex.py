#!/usr/bin/env python3
"""
cortex.py — LMDE cortical layer.

Receives CortexPulse UDP from Mint (Nazaré pathway decisions) and
integrates them with a slow EMA. Holds a stable symbolic state that
only transitions when evidence is sustained over many teaching events.

Mint teaches. LMDE remembers.

Port: 7410
"""
import socket
import struct
import time

LISTEN_PORT = 7410
_CP_FMT     = ">HffIf"
_CP_SIZE    = struct.calcsize(_CP_FMT)
_CP_MAGIC   = 0x4358

# Slow EMA — alpha=0.02 means ~50 events to move meaningfully
EMA_ALPHA   = 0.02
UNCERTAIN_BAND = 0.5   # |ema_x - ema_y| below this → UNCERTAIN

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", LISTEN_PORT))
print(f"[cortex] listening on :{LISTEN_PORT}  α={EMA_ALPHA}  uncertain_band=±{UNCERTAIN_BAND}")

ema_x     = 0.0
ema_y     = 0.0
state     = "UNCERTAIN"
events    = 0
last_flip = 0

while True:
    data, addr = sock.recvfrom(64)
    if len(data) < _CP_SIZE:
        continue
    magic, x, y, cycle, margin = struct.unpack_from(_CP_FMT, data)
    if magic != _CP_MAGIC:
        continue

    ema_x = ema_x * (1 - EMA_ALPHA) + x * EMA_ALPHA
    ema_y = ema_y * (1 - EMA_ALPHA) + y * EMA_ALPHA
    events += 1

    diff = ema_x - ema_y
    if abs(diff) < UNCERTAIN_BAND:
        new_state = "UNCERTAIN"
    elif diff > 0:
        new_state = "YES"
    else:
        new_state = "NO"

    if new_state != state:
        print(f"[cortex] *** STATE → {new_state} ***  "
              f"(was {state}, after {events - last_flip} events)")
        state     = new_state
        last_flip = events

    print(f"[cortex] event={events:4d}  mint_cycle={cycle:4d}  "
          f"ema_x={ema_x:.3f}  ema_y={ema_y:.3f}  "
          f"diff={diff:+.3f}  state={state}")
