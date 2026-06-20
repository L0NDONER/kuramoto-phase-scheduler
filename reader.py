"""
Kuramoto phase reader for Mint.

Listens to Pi + Pi2 beacons. Never transmits. Never couples.
Detects the Pi↔Pi2 drain crossing and fires a local tc burst window
so Mint's WAN traffic is timed without perturbing the oscillator pair.

Usage:
    sudo python3 reader.py
"""

import socket
import struct
import subprocess
import time
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

MCAST_GRP    = "239.0.0.1"
MCAST_PORT   = 7400
MAGIC        = 0x1B4A
FMT          = "!HBIff x"

PHASE_TARGET = math.pi      # true anti-phase target
ANTI_THRESH  = 0.20
LOCK_WINDOW  = 20
LOCK_STD     = 0.10

TC_BIN       = "/usr/sbin/tc"
TC_DEV       = "enp0s31f6"
TC_CLASS     = "1:20"
TC_RATE      = "20mbit"
TC_BURST_IDLE  = "64k"
TC_BURST_DRAIN = "500k"

_pool = ThreadPoolExecutor(max_workers=2)

def _tc_run(burst):
    cmd = [TC_BIN, "class", "change", "dev", TC_DEV,
           "classid", TC_CLASS, "htb", "rate", TC_RATE, "burst", burst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"\n[tc] {r.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"\n[tc] {e}", flush=True)

def open_drain():
    _tc_run(TC_BURST_DRAIN)
    time.sleep(0.20)
    _tc_run(TC_BURST_IDLE)

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", MCAST_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)

    phases     = {}   # pid -> theta
    prev_diff  = None
    history    = []
    drains     = 0

    print("[reader] passive — listening to Pi(1) + Pi2(2)", flush=True)

    while True:
        try:
            data, _ = s.recvfrom(64)
            if len(data) < struct.calcsize(FMT):
                continue
            magic, sid, tick, theta, omega = struct.unpack(FMT, data)
            if magic != MAGIC or sid not in (1, 2):
                continue

            phases[sid] = theta

            if len(phases) < 2:
                continue

            diff       = phases[1] - phases[2]
            phase_diff = abs((diff + math.pi) % (2 * math.pi) - math.pi)

            history.append(phase_diff)
            if len(history) > LOCK_WINDOW:
                history.pop(0)

            locked = (len(history) >= LOCK_WINDOW and
                      statistics.stdev(history) < LOCK_STD and
                      abs(phase_diff - PHASE_TARGET) < ANTI_THRESH)

            # fire drain when Pi↔Pi2 phase_diff descends through target
            # mint fires at the same crossing as pi — coordinated burst
            if prev_diff is not None and prev_diff > PHASE_TARGET and phase_diff <= PHASE_TARGET:
                _pool.submit(open_drain)
                drains += 1

            prev_diff = phase_diff

            filled = int(phase_diff / math.pi * 39)
            bar    = chr(9608) * filled + chr(9617) * (40 - filled)
            print(f"\r[reader] φ={phase_diff:.3f}  {bar}  {'LOCKED' if locked else '      '}  drains={drains}",
                  end="", flush=True)

        except BlockingIOError:
            time.sleep(0.001)

if __name__ == "__main__":
    main()
