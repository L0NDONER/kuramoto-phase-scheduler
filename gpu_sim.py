"""
Simulated GPU thermal emitter — runs on Pi or Pi2.

Listens to own beacon on the multicast group, maps phase → simulated
thermal watts, and sends telemetry to Mint's gpu_reader.

Usage:
    python3 gpu_sim.py pi
    python3 gpu_sim.py pi2
"""

import math
import socket
import struct
import sys
import time

MCAST_GRP   = "239.0.0.1"
MCAST_PORT  = 7400
BEACON_MAGIC = 0x1B4A
BEACON_FMT  = "!HBIff x"
BEACON_SIZE = struct.calcsize(BEACON_FMT)

MINT_IP     = "10.0.0.71"
TELEM_PORT  = 7401
TELEM_MAGIC = 0x6750
TELEM_FMT   = "!HBf"   # magic, sid, watts

GPU_IDLE_W  = 200.0
GPU_DELTA_W = 77.0     # gives 13.9% combined-peak reduction at anti-phase

SID_MAP = {"pi": 1, "pi2": 2}


def thermal(theta: float) -> float:
    return GPU_IDLE_W + GPU_DELTA_W * (1.0 - math.cos(theta)) / 2.0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SID_MAP:
        print("usage: gpu_sim.py pi|pi2")
        sys.exit(1)

    name = sys.argv[1]
    sid  = SID_MAP[name]

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", MCAST_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    rx.setblocking(False)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"[gpu_sim:{name}] idle={GPU_IDLE_W:.0f}W  delta={GPU_DELTA_W:.0f}W  peak={GPU_IDLE_W+GPU_DELTA_W:.0f}W")

    while True:
        try:
            data, _ = rx.recvfrom(64)
            if len(data) < BEACON_SIZE:
                continue
            magic, bsid, tick, theta, omega = struct.unpack(BEACON_FMT, data)
            if magic != BEACON_MAGIC or bsid != sid:
                continue

            watts = thermal(theta)
            tx.sendto(struct.pack(TELEM_FMT, TELEM_MAGIC, sid, watts), (MINT_IP, TELEM_PORT))

            print(f"\r[gpu_sim:{name}] θ={theta:.3f}  T={watts:.1f}W", end="", flush=True)

        except BlockingIOError:
            time.sleep(0.001)


if __name__ == "__main__":
    main()
