#!/usr/bin/env python3
"""
Phase-based GPU thermal reader.

Listens to Pi + Pi2 beacon multicast, extracts phase,
computes synthetic GPU thermal load locally, and reports:

- per-GPU watts
- combined watts
- rolling p95 peak
- % reduction vs in-phase baseline
- phase-alignment quality
"""

import collections
import math
import socket
import statistics
import struct
import time

MCAST_GRP    = "239.0.0.1"
BEACON_MAGIC = 0x1B4A
BEACON_FMT   = "!HBIff x"
BEACON_SIZE  = struct.calcsize(BEACON_FMT)
PORT         = 7400

GPU_IDLE_W  = 200.0
GPU_DELTA_W = 77.0
BASELINE    = 2 * (GPU_IDLE_W + GPU_DELTA_W)   # 554W in-phase peak

WINDOW = 120


def T(theta: float) -> float:
    return GPU_IDLE_W + GPU_DELTA_W * (1 - math.cos(theta)) / 2


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)

    phases  = {}   # sid -> theta
    history = collections.deque(maxlen=WINDOW)
    last_print = time.monotonic()

    print(f"[gpu_reader] baseline in-phase peak = {BASELINE:.0f}W")
    print(f"[gpu_reader] listening on :{PORT}")

    while True:
        try:
            data, _ = s.recvfrom(64)
            if len(data) < BEACON_SIZE:
                continue

            magic, sid, tick, theta, omega = struct.unpack(BEACON_FMT, data)
            if magic != BEACON_MAGIC or sid not in (1, 2):
                continue

            phases[sid] = theta

            if len(phases) < 2:
                continue

            w1 = T(phases[1])
            w2 = T(phases[2])
            combined = w1 + w2

            # phase_diff in [0, π]: π = anti-phase, 0 = in-phase
            phase_diff = abs((phases[1] - phases[2] + math.pi) % (2 * math.pi) - math.pi)
            quality    = phase_diff / math.pi * 100.0

            # only count samples where the pair looks genuinely anti-phase
            if quality > 92.0:
                history.append(combined)

            if len(history) < 20:
                continue

            p95       = statistics.quantiles(history, n=20)[18]
            reduction = (BASELINE - p95) / BASELINE * 100.0

            wmax = GPU_IDLE_W + GPU_DELTA_W
            bar1 = "█" * int(w1 / wmax * 20) + "░" * (20 - int(w1 / wmax * 20))
            bar2 = "█" * int(w2 / wmax * 20) + "░" * (20 - int(w2 / wmax * 20))

            now = time.monotonic()
            if now - last_print > 0.05:
                last_print = now
                print(
                    f"\r[gpu_reader]  Pi={w1:5.1f}W {bar1}  "
                    f"Pi2={w2:5.1f}W {bar2}  "
                    f"combined={combined:5.0f}W  p95={p95:5.0f}W  "
                    f"reduction={reduction:+.1f}%  quality={quality:4.0f}%  pd={phase_diff:.3f}/{math.pi:.3f}",
                    end="", flush=True,
                )

        except BlockingIOError:
            time.sleep(0.001)


if __name__ == "__main__":
    main()
