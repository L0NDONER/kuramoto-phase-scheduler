#!/usr/bin/env python3
"""
quartz_beacon.py — AxisPulse from real quartz, not simulated Kuramoto.

Replaces beacon.c. Each instance reads its own adjtimex freq_ppm (the
physical crystal's own drift — no coupling force, nothing steers it) and
derives a phase advancing at that crystal's natural period. Two instances
(pi, pi2) exchange raw phase on 239.0.0.1:7400 and each re-derives the
anti-phase (pd) between the two real oscillators, broadcasting the same
38-byte AxisPulse format nazare.py/reflex.py/bio.py already expect on
239.0.0.2:7404. Downstream consumers are unchanged.

Mint (sid=3) uses its 24MHz HPET crystal instead of adjtimex — see
hpet_ppm.py. adjtimex reports the kernel's NTP *correction*, not the
crystal's honest rate; HPET is diffed against CLOCK_MONOTONIC_RAW, which
NTP never slews, so it's a true third independent witness rather than
another node reporting a disciplined number.

Usage:
    sudo python3 quartz_beacon.py pi     # sid=1, adjtimex
    sudo python3 quartz_beacon.py pi2    # sid=2, adjtimex
    sudo python3 quartz_beacon.py mint   # sid=3, HPET
"""
import ctypes
import math
import selectors
import socket
import struct
import sys
import time

from hpet_ppm import Hpet, read_hpet_ppm

RAW_GRP   = "239.0.0.1"; RAW_PORT = 7400
AP_GRP    = "239.0.0.2"; AP_PORT  = 7404

RAW_MAGIC = 0x1B4A
RAW_FMT   = "!HBIffBQ"          # magic, sid, tick, theta, omega, _pad, t0_ns — matches reader_glyph.c Beacon struct (24 bytes)
RAW_SIZE  = struct.calcsize(RAW_FMT)

AP_MAGIC  = 0x4158
AP_FMT    = ">HBBIfffffHQ"      # magic,sid,locked,tick,theta1,theta2,pd,pd_dev,load_avg,drains,t0_ns
AP_SIZE   = struct.calcsize(AP_FMT)

OMEGA0        = 1.06     # rad/s — shared nominal carrier (beacon.c: ~0.053 rad / 50ms tick).
                          # ppm is a fractional correction on this carrier, not a carrier itself.
TICK_S        = 0.01     # 10ms
ANCHOR_EVERY  = 1000     # re-broadcast t0_ns every N ticks (~10s)
RESAMPLE_EVERY = 3000    # re-read own adjtimex every N ticks (~30s)
PEER_STALE_S  = 3.0
PD_EMA_ALPHA  = 0.20


class _timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_int), ("offset", ctypes.c_long),
                ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time", ctypes.c_long * 2),
                ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
                ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long),
                ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
                ("pad", ctypes.c_int * 11)]


def read_freq_ppm():
    tx = _timex()
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.adjtimex(ctypes.byref(tx)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return tx.freq / 65536.0


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def mcast_out(ttl=8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    return s


def wrap_pi(x):
    return (x + math.pi) % (2 * math.pi) - math.pi


SID_BY_HOST = {"pi": 1, "pi2": 2, "mint": 3}

CLOCK = time.CLOCK_MONOTONIC_RAW
now_raw = lambda: time.clock_gettime(CLOCK)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SID_BY_HOST:
        sys.exit("usage: quartz_beacon.py {pi|pi2|mint}")
    host = sys.argv[1]
    sid = SID_BY_HOST[host]

    hpet = Hpet() if host == "mint" else None
    resample = (lambda: read_hpet_ppm(hpet, sample_s=1.0)[0]) if hpet else read_freq_ppm

    ppm = resample()
    omega = OMEGA0 * (1.0 + ppm * 1e-6)
    print(f"[quartz] sid={sid} freq_ppm={ppm:.6f} omega0={OMEGA0:.6f} omega={omega:.9f}", flush=True)

    raw_in  = mcast_in(RAW_GRP, RAW_PORT)
    raw_out = mcast_out()
    ap_out  = mcast_out()

    sel = selectors.DefaultSelector()
    sel.register(raw_in, selectors.EVENT_READ, data="raw")

    t0 = now_raw()
    tick = 0
    peers = {}   # peer_sid -> {theta, omega, t, pd_prev, pd_ema}

    next_tick_at = now_raw()
    while True:
        timeout = max(0.0, next_tick_at - now_raw())
        for key, _ in sel.select(timeout=timeout):
          # Drain every datagram queued on this socket, not just one.
          # At 100Hz per sender, two-plus sources push more packets/sec
          # than this loop's own 100Hz tick cadence can drain one-per-
          # wakeup — the resulting backlog skews processing order enough
          # to spuriously trip the origin-pin's PEER_STALE_S check below,
          # which depends on accurate delivery timing in a way the old
          # unconditional-overwrite code never did. Origin pinning without
          # this is worse than no fix at all under real multi-source load
          # (measured: dev up to 0.94 pinned+starved vs 0.32 unpinned+
          # starved on the same two-real-source input).
          while True:
            try:
                data, addr = raw_in.recvfrom(64)
            except BlockingIOError:
                break
            if len(data) < RAW_SIZE:
                continue
            f = struct.unpack_from(RAW_FMT, data)
            if f[0] != RAW_MAGIC or f[1] == sid:
                continue
            psid = f[1]
            src_ip = addr[0]
            now_seen = now_raw()
            existing = peers.get(psid)
            # Pin this sid to whichever source IP is currently live for it.
            # A second source claiming the same sid while the bound source
            # is still fresh (within PEER_STALE_S) is rejected outright,
            # not blended — two sources racing for one slot is exactly the
            # 2026-07-09 corruption (peer1 alternating identity, dev
            # spiking 0.02-0.87 against a ~0.0001 baseline). Once the bound
            # source goes stale, a new IP is free to take the slot — this
            # is the one open assumption: that a legitimate source change
            # (DHCP renewal, node restart) always follows a full
            # PEER_STALE_S silence rather than overlapping the old source.
            if (existing is not None and existing.get("addr") is not None
                    and src_ip != existing["addr"]
                    and (now_seen - existing["t"]) < PEER_STALE_S):
                continue
            peer = peers.setdefault(psid, {"pd_prev": None, "pd_ema": 0.0, "addr": None})
            peer["theta"] = f[3]
            peer["omega"] = f[4]
            peer["t"] = now_seen
            peer["addr"] = src_ip

        now = now_raw()
        if now < next_tick_at:
            continue
        next_tick_at = now + TICK_S

        if tick and tick % RESAMPLE_EVERY == 0:
            new_ppm = resample()
            if new_ppm != ppm:
                theta_now = (omega * (now - t0)) % (2 * math.pi)
                ppm = new_ppm
                omega = OMEGA0 * (1.0 + ppm * 1e-6)
                # re-anchor t0 so the new omega reproduces theta_now at this
                # instant instead of snapping theta to 0 (old `t0 = now` was
                # not actually continuous — it discarded the phase reached
                # so far and restarted from zero)
                t0 = now - theta_now / omega
                print(f"[quartz] sid={sid} resampled freq_ppm={ppm:.6f} omega={omega:.9f}", flush=True)

        theta_self = (omega * (now - t0)) % (2 * math.pi)

        t0_ns = int(t0 * 1e9) if tick % ANCHOR_EVERY == 0 else 0
        raw_out.sendto(struct.pack(RAW_FMT, RAW_MAGIC, sid, tick, theta_self, omega, 0, t0_ns),
                        (RAW_GRP, RAW_PORT))

        live = {psid: p for psid, p in peers.items() if (now - p["t"]) < PEER_STALE_S}

        if live:
            for psid, p in live.items():
                pd = wrap_pi(theta_self - p["theta"])
                if p["pd_prev"] is not None:
                    p["pd_ema"] = PD_EMA_ALPHA * abs(pd - p["pd_prev"]) + (1 - PD_EMA_ALPHA) * p["pd_ema"]
                p["pd_prev"] = pd

                ap_pkt = struct.pack(AP_FMT, AP_MAGIC, sid, 1, tick,
                                      theta_self, p["theta"], pd, p["pd_ema"],
                                      0.0, 0, t0_ns)
                ap_out.sendto(ap_pkt, (AP_GRP, AP_PORT))
        else:
            ap_pkt = struct.pack(AP_FMT, AP_MAGIC, sid, 0, tick,
                                  theta_self, 0.0, 0.0, 0.0, 0.0, 0, t0_ns)
            ap_out.sendto(ap_pkt, (AP_GRP, AP_PORT))

        if tick % 100 == 0:
            if live:
                summary = " ".join(
                    f"peer{psid}(pd={p['pd_prev']:+.4f} dev={p['pd_ema']:.5f} omega={p['omega']:.4f})"
                    for psid, p in sorted(live.items())
                )
            else:
                summary = "no peers"
            print(f"[quartz] tick={tick} theta={theta_self:.4f} omega_self={omega:.4f} "
                  f"locked={bool(live)} {summary}", flush=True)

        tick += 1


if __name__ == "__main__":
    main()
