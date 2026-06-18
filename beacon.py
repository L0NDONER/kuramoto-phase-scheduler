"""
Kuramoto beacon exchange with tc drain shim.

Run on Mint:  python3 beacon.py mint
Run on Pi:    python3 beacon.py pi

Each side broadcasts its phase every 50ms, converges to anti-phase (φ→π),
then fires a tc burst on each drain window.
"""

import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
import math
import random
import sys
import statistics
from collections import deque

# ------------------------------------
# CONFIG
# ------------------------------------
MCAST_GRP   = "239.0.0.1"
MCAST_PORT  = 7400
TICK_S      = 0.05          # 20 Hz

OMEGA = {"mint": 0.048, "pi": 0.060}
NOISE       = 0.008
ANTI_THRESH = 0.25          # rad from π → considered locked (simple check)
LOCK_WINDOW = 20            # ticks of history for statistical lock detection
LOCK_STD    = 0.10          # max phase std to count as statistically locked
REMOTE_TIMEOUT = 0.5        # seconds before entering free-run mode

# tc shim — only active when role == "pi", shaping runs on Pi eth0
TC_DEV         = "eth0"
TC_CLASS       = "1:20"     # catch-all (Firestick + unclassified), 5 Mbps
TC_RATE        = "5mbit"
TC_BURST_IDLE  = "64k"      # normal burst
TC_BURST_DRAIN = "500k"     # burst window opened at drain event
DRAIN_TICKS    = 4          # base ticks to hold burst open (~200ms)

# Beacon struct: magic(H) sender(B) tick(I) theta(f) omega(f) + pad = 16 bytes
MAGIC      = 0x1B4A
FMT        = "!HBIff x"
SENDER_ID  = {"mint": 0, "pi": 1}

# ------------------------------------
# TC SHIM — threaded subprocess (non-blocking tick loop)
# ------------------------------------
TC_BIN      = "/usr/sbin/tc"
_tc_pool    = None

def _tc_run(burst):
    cmd = [TC_BIN, "class", "change", "dev", TC_DEV,
           "classid", TC_CLASS, "htb", "rate", TC_RATE, "burst", burst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"\n[tc] error: {r.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"\n[tc] exception: {e}", flush=True)

def _tc_init():
    global _tc_pool
    _tc_pool = ThreadPoolExecutor(max_workers=2)

def open_drain():
    if _tc_pool:
        _tc_pool.submit(_tc_run, TC_BURST_DRAIN)

def close_drain():
    if _tc_pool:
        _tc_pool.submit(_tc_run, TC_BURST_IDLE)

# ------------------------------------
# SOCKET SETUP
# ------------------------------------
def make_sender():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    return s

def make_receiver(own_id):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", MCAST_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)
    return s

def pack_beacon(sender_id, tick, theta, omega):
    return struct.pack(FMT, MAGIC, sender_id, tick, theta, omega)

def unpack_beacon(data):
    if len(data) < struct.calcsize(FMT):
        return None
    magic, sender_id, tick, theta, omega = struct.unpack(FMT, data)
    if magic != MAGIC:
        return None
    return sender_id, tick, theta, omega

# ------------------------------------
# MAIN
# ------------------------------------
def run(role):
    own_id    = SENDER_ID[role]
    remote_id = 1 - own_id
    is_pi     = role == "pi"

    theta      = 0.0 if role == "mint" else math.pi / 3
    omega      = OMEGA[role]
    tick       = 0

    remote_theta      = None
    remote_tick_seen  = -1
    last_remote_theta = None
    last_remote_tick  = -1
    last_beacon_time  = None
    omega_error       = 0.0

    phase_history  = deque(maxlen=LOCK_WINDOW)
    prev_theta_mod = None
    drain_ttl      = 0
    drain_count    = 0

    tx = make_sender()
    rx = make_receiver(own_id)

    print(f"[{role}] started — tx→ {MCAST_GRP}:{MCAST_PORT}  omega={omega:.4f}")
    if is_pi:
        _tc_init()
        print(f"[{role}] tc shim active — {TC_DEV} class {TC_CLASS}  drain burst={TC_BURST_DRAIN}")

    while True:
        t0 = time.monotonic()

        # --- receive remote beacon ---
        try:
            while True:
                data, addr = rx.recvfrom(64)
                parsed = unpack_beacon(data)
                if parsed and parsed[0] == remote_id:
                    _, rtick, rtheta, _ = parsed
                    if rtick > remote_tick_seen:
                        # frequency tracking
                        if last_remote_theta is not None and rtick > last_remote_tick:
                            dt       = rtick - last_remote_tick
                            measured = ((rtheta - last_remote_theta + math.pi) % (2 * math.pi) - math.pi) / dt
                            omega_error = 0.9 * omega_error + 0.1 * (measured - omega)
                            omega       = max(0.01, omega + omega_error * 0.01)
                        last_remote_theta = rtheta
                        last_remote_tick  = rtick
                        last_beacon_time  = time.monotonic()
                        remote_theta      = rtheta
                        remote_tick_seen  = rtick
        except BlockingIOError:
            pass

        # --- graceful degradation: free-run if remote gone ---
        remote_alive = last_beacon_time is not None and (time.monotonic() - last_beacon_time) < REMOTE_TIMEOUT
        if not remote_alive:
            theta = (theta + omega) % (2 * math.pi)
            pkt = pack_beacon(own_id, tick, theta, omega)
            tx.sendto(pkt, (MCAST_GRP, MCAST_PORT))
            print(f"\r[{role}] tick={tick:5d}  free-run (no remote)   ", end="", flush=True)
            tick += 1
            elapsed = time.monotonic() - t0
            wait = TICK_S - elapsed
            if wait > 0:
                time.sleep(wait)
            continue

        # --- Kuramoto step ---
        diff        = remote_theta - theta
        error       = abs((diff + math.pi) % (2 * math.pi) - math.pi)
        k           = min(0.05, 0.01 + error * 0.1)
        noise       = random.gauss(0, NOISE)
        dtheta      = omega - k * math.sin(diff) + noise
        theta       = (theta + dtheta) % (2 * math.pi)

        # --- statistical lock detection ---
        phase_diff = abs((remote_theta - theta + math.pi) % (2 * math.pi) - math.pi)
        phase_history.append(phase_diff)
        if len(phase_history) >= LOCK_WINDOW // 2:
            locked = statistics.stdev(phase_history) < LOCK_STD and phase_diff > (math.pi - ANTI_THRESH)
        else:
            locked = False

        # --- adaptive drain window (Pi only) ---
        if is_pi and locked:
            curr_mod = theta % math.pi
            if prev_theta_mod is not None and curr_mod < prev_theta_mod:
                if drain_ttl == 0:
                    drain_count += 1
                    # wider window when farther from π, tighter when close
                    drain_ttl = int(DRAIN_TICKS * (1 + abs(phase_diff - math.pi) / math.pi))
                    open_drain()
            prev_theta_mod = curr_mod

            if drain_ttl > 0:
                drain_ttl -= 1
                if drain_ttl == 0:
                    close_drain()
        elif is_pi and not locked and prev_theta_mod is not None:
            # lost lock — reset drain state cleanly
            if drain_ttl > 0:
                drain_ttl = 0
                close_drain()
            prev_theta_mod = None

        # --- transmit beacon ---
        pkt = pack_beacon(own_id, tick, theta, omega)
        tx.sendto(pkt, (MCAST_GRP, MCAST_PORT))

        # --- report ---
        bar      = int(phase_diff / math.pi * 40)
        status   = "LOCKED" if locked else "      "
        drain_str = f"  drains={drain_count}" if is_pi else ""
        print(f"\r[{role}] tick={tick:5d}  φ={phase_diff:.3f}  {'█'*bar}{'░'*(40-bar)}  {status}{drain_str}   ", end="", flush=True)

        tick += 1

        # --- sleep remainder of tick ---
        elapsed = time.monotonic() - t0
        wait    = TICK_S - elapsed
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("mint", "pi"):
        print("usage: python3 beacon.py mint|pi")
        sys.exit(1)
    try:
        run(sys.argv[1])
    finally:
        if sys.argv[1] == "pi":
            close_drain()
