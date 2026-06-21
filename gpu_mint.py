"""
Standalone Mint GPU follower.

Reads Pi1 + Pi2 beacons from multicast.
Evolves a local Kuramoto oscillator — never transmits.
Computes T(theta) and logs thermal output.

Usage:
    python3 gpu_mint.py 1   # lock to Pi1
    python3 gpu_mint.py 2   # lock to Pi2 (other anti-phase slot)
"""

import math
import random
import socket
import struct
import sys
import time

MCAST_GRP    = "239.0.0.1"
PORT         = 7400
MAGIC        = 0x1B4A
FMT          = "!HBIff x"
SIZE         = struct.calcsize(FMT)

OMEGA        = 0.052
K            = 0.30
NOISE        = 0.002
TICK_S       = 0.05

GPU_IDLE_W   = 200.0   # baseline
GPU_DELTA_W  = 77.0   # baseline


def T(theta):
    return GPU_IDLE_W + GPU_DELTA_W * (1 - math.cos(theta)) / 2


def main():
    target_sid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    heat_pct   = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    GPU_IDLE_W   = 200.0 * (1 + heat_pct / 100)
    GPU_DELTA_W  = 77.0  * (1 + heat_pct / 100)
    name = f"gpu_mint{target_sid}({'%+.0f' % heat_pct}%)"

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    rx.setblocking(False)

    theta        = 0.0
    remote_theta = {}
    tick         = 0

    T = lambda theta: GPU_IDLE_W + GPU_DELTA_W * (1 - math.cos(theta)) / 2
    print(f"[{name}] follower → Pi{target_sid}  idle={GPU_IDLE_W:.1f}W  peak={GPU_IDLE_W+GPU_DELTA_W:.1f}W")

    while True:
        t0 = time.monotonic()

        try:
            while True:
                data, _ = rx.recvfrom(64)
                if len(data) < SIZE:
                    continue
                magic, sid, _, rtheta, _ = struct.unpack(FMT, data)
                if magic != MAGIC or sid not in (1, 2):
                    continue
                remote_theta[sid] = rtheta
        except BlockingIOError:
            pass

        if target_sid in remote_theta:
            coupling = math.sin(remote_theta[target_sid] - theta)
            theta = (theta + OMEGA + K * coupling + random.gauss(0, NOISE)) % (2 * math.pi)
        else:
            theta = (theta + OMEGA) % (2 * math.pi)

        watts = T(theta)

        pd1 = pd2 = None
        if 1 in remote_theta:
            pd1 = abs((remote_theta[1] - theta + math.pi) % (2 * math.pi) - math.pi)
        if 2 in remote_theta:
            pd2 = abs((remote_theta[2] - theta + math.pi) % (2 * math.pi) - math.pi)

        bar = "█" * int(watts / (GPU_IDLE_W + GPU_DELTA_W) * 20) + "░" * (20 - int(watts / (GPU_IDLE_W+GPU_DELTA_W) * 20))
        pd_str = f"  pd1={pd1:.3f}  pd2={pd2:.3f}" if pd1 and pd2 else ""
        print(f"\r[{name}] tick={tick:6d}  θ={theta:.3f}  T={watts:5.1f}W {bar}{pd_str}   ", end="", flush=True)

        tick += 1
        elapsed = time.monotonic() - t0
        wait = TICK_S - elapsed
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    main()
