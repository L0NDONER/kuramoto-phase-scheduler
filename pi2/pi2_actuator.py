#!/usr/bin/env python3
"""
pi2_actuator.py — cgroup + stress-ng actuator for Pi2.

Listens UDP :7431 for ASCII intents from pi2_reader:
  "PARK"   → park one core (shrink cpuset, restart workers)
  "UNPARK" → unpark one core (expand cpuset, restart workers)
  "HOLD"   → no-op

Workers always match active core count.
Shrink: stop workers → shrink cpuset → start fewer.
Expand: expand cpuset → stop workers → start more.

Usage: sudo python3 pi2_actuator.py [--load PCT] [--cores-start N]
"""
import socket, subprocess, os, sys, atexit, time

INTENT_PORT  = 7431
CGROUP       = "/sys/fs/cgroup/shaper"
CORES_MIN    = 1
CORES_MAX    = 4
DWELL_SECS   = 15

LOAD_PCT     = 60
CORES_START  = 4

for i, arg in enumerate(sys.argv[1:]):
    if arg == "--load"         and i+2 < len(sys.argv): LOAD_PCT     = int(sys.argv[i+2])
    if arg == "--cores-start"  and i+2 < len(sys.argv): CORES_START  = int(sys.argv[i+2])


def _cg_write(rel, val):
    try:
        with open(f"{CGROUP}/{rel}", "w") as f:
            f.write(str(val))
    except OSError as e:
        print(f"[actuator] cgroup {rel}={val} failed: {e}", flush=True)

def _cpus_str(n):
    return "0" if n == 1 else f"0-{n-1}"

def setup_cgroup(n):
    os.makedirs(CGROUP, exist_ok=True)
    _cg_write("cpuset.mems", "0")
    _cg_write("cpuset.cpus", _cpus_str(n))

def set_cpuset(n):
    _cg_write("cpuset.cpus", _cpus_str(n))

def move_to_cgroup(pid):
    _cg_write("cgroup.procs", pid)

def current_cores():
    try:
        s = open(f"{CGROUP}/cpuset.cpus").read().strip()
        return int(s.split("-")[1]) + 1 if "-" in s else 1
    except OSError:
        return CORES_START


_stress = None

def start_stress(n):
    global _stress
    p = subprocess.Popen(
        ["stress-ng", "--cpu", str(n), "--cpu-load", str(LOAD_PCT), "--timeout", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _stress = p
    time.sleep(0.05)
    move_to_cgroup(p.pid)
    return p

def stop_stress():
    global _stress
    if _stress and _stress.poll() is None:
        _stress.terminate()
        try:
            _stress.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _stress.kill()
    _stress = None

def park_one(cores):
    n = max(CORES_MIN, cores - 1)
    if n == cores:
        return cores
    stop_stress(); set_cpuset(n); start_stress(n)
    return n

def unpark_one(cores):
    n = min(CORES_MAX, cores + 1)
    if n == cores:
        return cores
    set_cpuset(n); stop_stress(); start_stress(n)
    return n


cores = CORES_START
setup_cgroup(cores)

def _cleanup():
    stop_stress()
    try: _cg_write("cpuset.cpus", "0-3")
    except: pass
atexit.register(_cleanup)

_stress_proc = start_stress(cores)
print(f"[actuator] stress-ng pid={_stress_proc.pid}  cores={cores}  load={LOAD_PCT}%", flush=True)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", INTENT_PORT))
sock.settimeout(1.0)

print(f"[actuator] listening on 127.0.0.1:{INTENT_PORT}  dwell={DWELL_SECS}s", flush=True)

last_change  = 0.0
pending      = None   # current candidate intent
pending_since = 0.0
votes_for    = 0
votes_against = 0

while True:
    if _stress and _stress.poll() is not None:
        print("[actuator] stress-ng died — restarting", flush=True)
        start_stress(cores)

    try:
        data, _ = sock.recvfrom(64)
    except socket.timeout:
        continue

    intent = data.decode(errors="ignore").strip().upper()
    if intent not in ("PARK", "UNPARK", "HOLD"):
        print(f"[actuator] unknown intent: {intent!r}", flush=True)
        continue

    now = time.time()

    # Still in post-commit cooldown — ignore everything
    if now - last_change < DWELL_SECS:
        continue

    if intent == "HOLD":
        pending = None
        votes_for = votes_against = 0
        continue

    if pending is None:
        # Start accumulating for this intent
        pending       = intent
        pending_since = now
        votes_for     = 1
        votes_against = 0
        continue

    if intent == pending:
        votes_for += 1
    else:
        votes_against += 1
        # Majority flipped — reset to new direction
        if votes_against > votes_for:
            pending       = intent
            pending_since = now
            votes_for     = 1
            votes_against = 0
            continue

    # Commit when: pending held for DWELL_SECS AND still majority
    if now - pending_since >= DWELL_SECS and votes_for > votes_against:
        prev  = cores
        cores = park_one(cores) if pending == "PARK" else unpark_one(cores)
        if cores != prev:
            last_change = now
            ratio = votes_for / max(1, votes_for + votes_against)
            print(f"[actuator] {pending}  {prev} → {cores}"
                  f"  (held {now-pending_since:.0f}s  {ratio:.0%} agreement)", flush=True)
        pending = None
        votes_for = votes_against = 0
