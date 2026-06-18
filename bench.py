"""
Passive multicast listener — logs phase data and prints stats.
Joins the same multicast group as beacon.py but never transmits.

Usage:
    python3 bench.py <role_to_watch> <duration_seconds>
    python3 bench.py pi2 120
"""

import socket
import struct
import time
import sys
import math
import statistics

MCAST_GRP  = "239.0.0.1"
MCAST_PORT = 7400
MAGIC      = 0x1B4A
FMT        = "!HBIff x"
SENDER_ID  = {"mint": 0, "pi": 1, "pi2": 2}
PHASE_TARGET = 3.0
ANTI_THRESH  = 0.20
LOCK_WINDOW  = 20
LOCK_STD     = 0.10

def main():
    if len(sys.argv) != 3:
        print("usage: python3 bench.py <role> <seconds>")
        sys.exit(1)
    watch_role = sys.argv[1]
    duration   = int(sys.argv[2])
    watch_id   = SENDER_ID[watch_role]

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", MCAST_PORT))
    mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(1.0)

    print(f"Listening for [{watch_role}] beacons for {duration}s ...", flush=True)

    # track remote phases per peer so we can compute phase_diff
    remote_phases = {}
    samples = []       # (tick, phi, locked)
    phase_history = []
    deadline = time.monotonic() + duration
    last_tick = -1

    while time.monotonic() < deadline:
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            continue
        if len(data) < struct.calcsize(FMT):
            continue
        magic, sid, tick, theta, omega = struct.unpack(FMT, data)
        if magic != MAGIC:
            continue

        if sid != watch_id:
            remote_phases[sid] = theta
            continue

        if tick <= last_tick:
            continue
        last_tick = tick

        if not remote_phases:
            continue

        ref_theta  = next(iter(remote_phases.values()))
        phase_diff = abs((ref_theta - theta + math.pi) % (2 * math.pi) - math.pi)

        phase_history.append(phase_diff)
        if len(phase_history) > LOCK_WINDOW:
            phase_history.pop(0)

        if len(phase_history) >= LOCK_WINDOW // 2:
            locked = (statistics.stdev(phase_history) < LOCK_STD and
                      abs(phase_diff - PHASE_TARGET) < ANTI_THRESH)
        else:
            locked = False

        samples.append((tick, phase_diff, locked))
        elapsed = duration - (deadline - time.monotonic())
        print(f"\r  tick={tick:5d}  φ={phase_diff:.3f}  {'LOCKED' if locked else '      '}  [{elapsed:.0f}s/{duration}s]   ",
              end="", flush=True)

    print()
    if not samples:
        print("No samples collected.")
        return

    phis   = [s[1] for s in samples]
    locked = [s[2] for s in samples]
    pct    = 100 * sum(locked) / len(locked)

    print(f"\n=== {watch_role} benchmark ({duration}s, {len(samples)} ticks) ===")
    print(f"  mean φ   : {statistics.mean(phis):.4f}  (π = {math.pi:.4f})")
    print(f"  std  φ   : {statistics.stdev(phis):.4f} rad")
    print(f"  min  φ   : {min(phis):.4f}")
    print(f"  max  φ   : {max(phis):.4f}")
    print(f"  locked   : {sum(locked)}/{len(locked)}  ({pct:.1f}%)")
    print(f"  Δ from π : {abs(statistics.mean(phis) - math.pi):.4f} rad  "
          f"({100*abs(statistics.mean(phis)-math.pi)/math.pi:.2f}%)")

if __name__ == "__main__":
    main()
