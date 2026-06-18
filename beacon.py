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

OMEGA = {"mint": 0.052, "pi": 0.056, "pi2": 0.054}
NOISE       = 0.008
PHASE_TARGET = 3.0          # natural lock point for this Δω/K ratio
ANTI_THRESH  = 0.20         # rad from target → considered locked (simple check)
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
SENDER_ID  = {"mint": 0, "pi": 1, "pi2": 2}

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

    n_peers = len(SENDER_ID) - 1
    remote_theta      = {}   # peer_id -> latest theta
    remote_tick_seen  = {}   # peer_id -> latest tick
    last_remote_theta = {}
    last_remote_tick  = {}
    last_beacon_time  = {}
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
                if parsed and parsed[0] != own_id:
                    pid, rtick, rtheta, _ = parsed
                    if rtick > remote_tick_seen.get(pid, -1):
                        if pid in last_remote_theta and rtick > last_remote_tick.get(pid, -1):
                            dt       = rtick - last_remote_tick[pid]
                            measured = ((rtheta - last_remote_theta[pid] + math.pi) % (2 * math.pi) - math.pi) / dt
                            omega_error = 0.9 * omega_error + 0.1 * (measured - omega)
                            omega       = max(0.01, omega + omega_error * 0.001)
                        last_remote_theta[pid] = rtheta
                        last_remote_tick[pid]  = rtick
                        last_beacon_time[pid]  = time.monotonic()
                        remote_theta[pid]      = rtheta
                        remote_tick_seen[pid]  = rtick
        except BlockingIOError:
            pass

        # --- graceful degradation: free-run if no remotes alive ---
        now = time.monotonic()
        remote_alive = any((now - t) < REMOTE_TIMEOUT for t in last_beacon_time.values())
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

        # --- Kuramoto step (sum over all live peers) ---
        now = time.monotonic()
        live_peers = [pid for pid, t in last_beacon_time.items() if (now - t) < REMOTE_TIMEOUT]
        # pi2 couples to mint only — coupling to anti-phase mint+pi pair cancels to zero
        if own_id == 2 and 0 in live_peers:
            live_peers = [0]
        coupling = sum(math.sin(remote_theta[pid] - theta) for pid in live_peers)
        # phase_diff vs first live peer for lock/display
        ref_pid    = live_peers[0]
        diff       = remote_theta[ref_pid] - theta
        phase_diff = abs((diff + math.pi) % (2 * math.pi) - math.pi)
        error      = abs(phase_diff - PHASE_TARGET)
        k          = max(0.12, min(0.16, 0.12 + error * 0.1))
        noise      = random.gauss(0, NOISE)
        dtheta     = omega - k * coupling + noise
        theta      = (theta + dtheta) % (2 * math.pi)

        # --- statistical lock detection ---
        phase_diff = abs((remote_theta[ref_pid] - theta + math.pi) % (2 * math.pi) - math.pi)
        phase_history.append(phase_diff)
        if len(phase_history) >= LOCK_WINDOW // 2:
            locked = statistics.stdev(phase_history) < LOCK_STD and abs(phase_diff - PHASE_TARGET) < ANTI_THRESH
        else:
            locked = False

        # --- adaptive drain window (Pi only) ---
        if is_pi and locked:
            curr_mod = theta % math.pi
            if prev_theta_mod is not None and curr_mod < prev_theta_mod:
                if drain_ttl == 0:
                    drain_count += 1
                    # wider window when farther from π, tighter when close
                    drain_ttl = int(DRAIN_TICKS * (1 + abs(phase_diff - PHASE_TARGET) / math.pi))
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
    import os, atexit
    if len(sys.argv) != 2 or sys.argv[1] not in ("mint", "pi", "pi2"):
        print("usage: python3 beacon.py mint|pi")
        sys.exit(1)
    role = sys.argv[1]
    PIDFILE = f"/tmp/beacon-{role}.pid"
    if os.path.exists(PIDFILE):
        try:
            existing = int(open(PIDFILE).read())
            os.kill(existing, 0)
            print(f"beacon.py {role} already running (pid {existing}) — refusing to start")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(os.unlink, PIDFILE)
    try:
        run(role)
    finally:
        if role == "pi":
            close_drain()
