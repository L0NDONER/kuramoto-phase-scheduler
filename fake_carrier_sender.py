#!/usr/bin/env python3
"""
fake_carrier_sender.py — adversarial control for carrier_verify.py.

Gets exactly one real AxisPulse observation to learn the current tick
number and AP_TICK_S (which is public anyway — it's a constant in
quartz_beacon.py), then goes dark: no further subscription, just a
local sleep loop replaying the known cadence, same trick
adversary_sender.py used against verify_physical(). Tests whether
knowing the protocol is enough, or whether carrier_verify.py actually
requires *continued* live access to the carrier.
"""
import argparse
import hashlib
import selectors
import socket
import struct
import sys
import time

AP_GRP, AP_PORT = "239.0.0.2", 7404
AP_FMT = ">HBBIfffffHQ"
AP_MAGIC = 0x4158
AP_TICK_S = 0.01

NC_MAGIC = 0x4E43
NC_START, NC_PULSE, NC_END, NC_HEARTBEAT = 1, 2, 3, 4
NC_START_FMT = ">HBHHQ"
NC_PULSE_FMT = ">HBH"
NC_HEARTBEAT_FMT = ">HBQ"
NC_PORT = 7480


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="127.0.0.1")
    ap.add_argument("--sid", type=int, default=3)
    ap.add_argument("--beat-ticks", type=int, default=20)
    ap.add_argument("--hb-ticks", type=int, default=0,
                     help="fake heartbeats too, guessed tick numbers from the same dead-reckoned cadence")
    ap.add_argument("--salt", default="fake")
    ap.add_argument("--ap-port", type=int, default=AP_PORT)
    args = ap.parse_args()

    H = hashlib.sha256(args.salt.encode()).digest()
    bits = []
    for byte in H:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    n_bits = len(bits)

    # one-time snapshot only — this is the "leaked one packet" scenario
    ap_in = mcast_in(AP_GRP, args.ap_port)
    sel = selectors.DefaultSelector()
    sel.register(ap_in, selectors.EVENT_READ)
    latest_tick = None
    t_wait = time.monotonic() + 10.0
    while latest_tick is None and time.monotonic() < t_wait:
        for key, _ in sel.select(timeout=1.0):
            data, _ = ap_in.recvfrom(64)
            if len(data) < struct.calcsize(AP_FMT):
                continue
            f = struct.unpack_from(AP_FMT, data)
            if f[0] != AP_MAGIC or f[1] != args.sid:
                continue
            latest_tick = f[3]
    if latest_tick is None:
        sys.exit("[fake-carrier] couldn't even get the one snapshot")
    ap_in.close()  # go dark — no further live access from here on

    start_ap_tick = latest_tick + args.beat_ticks
    beat_s = args.beat_ticks * AP_TICK_S
    print(f"[fake-carrier] snapshot only: current_tick={latest_tick}  going dark, "
          f"free-running on known cadence beat_s={beat_s*1000:.0f}ms  hash={H.hex()[:16]}...", flush=True)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    start_pkt = struct.pack(NC_START_FMT, NC_MAGIC, NC_START, n_bits, args.beat_ticks, start_ap_tick)
    out.sendto(start_pkt, (args.dest, NC_PORT))

    t0 = time.monotonic()
    n_pulses = n_hb = 0
    hb_s = args.hb_ticks * AP_TICK_S if args.hb_ticks else None
    next_hb_at = t0 + hb_s if hb_s else None

    for i, b in enumerate(bits):
        target = t0 + (i + 1) * beat_s

        # dead-reckon heartbeats too, purely from elapsed wall time — no
        # live feed to check against, just the same guessed cadence
        while next_hb_at is not None and next_hb_at <= target:
            gap = next_hb_at - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            dead_reckoned_tick = latest_tick + round((time.monotonic() - t0) / AP_TICK_S)
            out.sendto(struct.pack(NC_HEARTBEAT_FMT, NC_MAGIC, NC_HEARTBEAT, dead_reckoned_tick),
                       (args.dest, NC_PORT))
            n_hb += 1
            next_hb_at += hb_s

        gap = target - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        if b:
            out.sendto(struct.pack(NC_PULSE_FMT, NC_MAGIC, NC_PULSE, i), (args.dest, NC_PORT))
            n_pulses += 1
        print("█" if b else "░", end="", flush=True)

    out.sendto(struct.pack(">HB", NC_MAGIC, NC_END), (args.dest, NC_PORT))
    print(f"\n[fake-carrier] END  {n_pulses}/{n_bits} pulsed  {n_hb} fake heartbeats  hash={H.hex()}", flush=True)


if __name__ == "__main__":
    main()
