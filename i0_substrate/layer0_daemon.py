#!/usr/bin/env python3
"""
layer0_shared_substrate/layer0_daemon.py -- the shared Layer0 substrate
itself. Standalone: no real domain binds to it yet (see README.md,
open question #2 -- deliberately not answered by this file).

Broadcasts a free-running tick + phase (theta) over LAN multicast at a
fixed cadence. Any number of listeners can bind and observe the exact
same (tick, theta, t0) at the exact same wall-clock moment -- that's
the "shared tick geometry" half of the touch definition in CLAUDE.md.

pd (added 2026-09-01) is self-jitter: actual discrete-tick theta vs. the
ideal continuous phase implied by real elapsed wall-clock time. This is
a genuine physical quantity -- real OS scheduling delay, GC pauses,
anything that makes a tick fire late shows up here -- not a fabricated
number. It's deliberately NOT cross-peer comparison (that's what
AxisPulse's pd is, comparing two independent oscillators); this daemon
still has no second real source bound to it, so a cross-peer pd would
still be fake. Self-jitter was chosen specifically because it's real
without needing one. See CLAUDE.md / README.md for why this was asked
for explicitly: it's what phase_auth-style presence-proof would
challenge against -- an attacker computing values in advance can't
predict this instant's real scheduling noise.

Still NOT included in this v1:
  - No presence-proof challenge/response itself (phase_auth's job, and
    phase_auth's LAN-adjacency security properties are a separate,
    already-solved problem -- this daemon is the thing something like
    phase_auth would eventually challenge against, not a reimplementation
    of phase_auth itself).
  - No domain binding/registration. This just broadcasts. "Touch" (a
    domain reading + gating through this) is entirely the listener's
    job -- see layer0_listen.py for the reference reader.

Wire format (network byte order): magic(H) tick(I) theta(f) pd(f) t0(d)
  magic  -- 0x4C30 ("L0"), distinguishes from every other multicast
            group/format in this repo (AxisPulse=0x4158, phase-auth
            challenge=0x5043, etc.)
  tick   -- monotonic counter, starts at 0 when this daemon starts.
            NOT a shared clock across restarts -- a listener that
            binds after a restart sees a lower tick than before. This
            mirrors AxisPulse's own tick semantics (per-node counter,
            only meaningful paired with a specific daemon instance).
  theta  -- free-running phase in [0, 2*pi), advances by a fixed
            increment each tick. No coupling, no feedback -- this is
            intentionally the simplest possible "phase envelope",
            not a Kuramoto oscillator (there's nothing to couple to
            yet with zero other real sources bound).
  pd     -- self-jitter: shortest angular distance between actual
            (discrete, tick-counted) theta and ideal (continuous,
            wall-clock-derived) theta at the moment of emission.
            ~0 when the tick loop is keeping up; nonzero when real
            scheduling delay has accumulated. Signed (direction
            matters: behind vs. ahead of ideal).
  t0     -- wall-clock time (time.time()) this tick was emitted, for
            listener-side staleness/freshness computation.

Usage:
    python3 layer0_daemon.py [--group 239.0.0.6] [--port 7500] [--tick-hz 20]
"""
import argparse
import math
import socket
import struct
import sys
import time

MAGIC = 0x4C30
FMT = "!HIffd"   # magic, tick, theta, pd, t0
PKT_SIZE = struct.calcsize(FMT)
TAU = 2 * math.pi


def angdiff(a, b):
    """Shortest signed angular distance from b to a, in (-pi, pi]."""
    return (a - b + math.pi) % TAU - math.pi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="239.0.0.6")
    ap.add_argument("--port", type=int, default=7500)
    ap.add_argument("--tick-hz", type=float, default=20.0,
                     help="ticks per second (matches AxisPulse-ish cadence; 20Hz = 50ms/tick)")
    ap.add_argument("--theta-step", type=float, default=0.31,
                     help="radians advanced per tick (arbitrary, just needs to be nonzero and not a divisor of 2*pi)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    tick = 0
    theta = 0.0
    tick_period = 1.0 / args.tick_hz
    # ideal phase advances continuously with wall-clock time, at the
    # same nominal rate as the discrete tick counter -- theta and
    # ideal_theta agree exactly only if every tick fires exactly on
    # schedule. Real scheduling jitter makes them diverge; pd is that
    # divergence.
    rate = args.theta_step / tick_period   # radians per real second

    print(f"[layer0] broadcasting on {args.group}:{args.port} at {args.tick_hz}Hz "
          f"(tick_period={tick_period*1000:.1f}ms)", flush=True)
    print(f"[layer0] wire format: magic={MAGIC:#06x} '{FMT}' size={PKT_SIZE}B", flush=True)

    start_monotonic = time.monotonic()
    next_tick_at = start_monotonic
    try:
        while True:
            now_wall = time.time()
            elapsed = time.monotonic() - start_monotonic
            ideal_theta = (elapsed * rate) % TAU
            pd = angdiff(theta, ideal_theta)

            pkt = struct.pack(FMT, MAGIC, tick, theta, pd, now_wall)
            sock.sendto(pkt, (args.group, args.port))

            if tick % (int(args.tick_hz) * 5) == 0:   # heartbeat log every ~5s
                print(f"[layer0] tick={tick} theta={theta:.4f} pd={pd:+.4f}", flush=True)

            tick += 1
            theta = (theta + args.theta_step) % TAU

            next_tick_at += tick_period
            sleep_s = next_tick_at - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                next_tick_at = time.monotonic()   # fell behind, resync instead of spinning
    except KeyboardInterrupt:
        print("\n[layer0] stopped", flush=True)


if __name__ == "__main__":
    main()
