#!/usr/bin/env python3
"""
quartz_status.py — one-shot substrate snapshot.

Samples RAW (omega), AxisPulse (pd_dev), and RefleState (reflex) for a
few seconds, then prints a table and exits. Run this instead of writing
a one-off multicast probe.

Competence boundary: none of these three wire formats carry an HMAC, so
nothing printed here is authenticated — a magic-number match only proves
"bytes are framed", not who sent them or that the values are honest. RAW
and AxisPulse do carry a tick field, so this tool uses it to prove
ordering (never display a packet older than the last one already shown
for that sid) — but tick order is not wall-clock freshness; there is no
timebase check here, so "the latest tick we've seen" could still be
stale if the sender itself stalled. RefleState carries no tick at all,
so for reflex there is no ordering proof available at any price: it is
the last envelope-valid packet received, in whatever order UDP happened
to deliver it, and is labelled as such rather than printed as a verdict.
"""
import selectors
import socket
import struct
import sys
import time

RAW_GRP, RAW_PORT = "239.0.0.1", 7400
RAW_FMT = "!HBIffBQ"; RAW_MAGIC = 0x1B4A

AP_GRP, AP_PORT = "239.0.0.2", 7404
AP_FMT = ">HBBIfffffHQ"; AP_MAGIC = 0x4158

RS_GRP, RS_PORT = "239.0.0.4", 7450
RS_FMT = "!HBff"; RS_MAGIC = 0x5253
RS_NAME = ["CALM", "ALERT", "WITHDRAW", "PARK", "RECOVER"]

WINDOW_S = 3.0


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
    raw = mcast_in(RAW_GRP, RAW_PORT)
    ap  = mcast_in(AP_GRP, AP_PORT)
    rs  = mcast_in(RS_GRP, RS_PORT)

    sel = selectors.DefaultSelector()
    sel.register(raw, selectors.EVENT_READ, data="raw")
    sel.register(ap,  selectors.EVENT_READ, data="ap")
    sel.register(rs,  selectors.EVENT_READ, data="rs")

    omega = {}       # sid -> omega
    omega_tick = {}  # sid -> tick that omega came from — guards against a
                     # reordered UDP packet silently overwriting a newer
                     # reading with a stale one
    pd_dev = {}      # sid -> latest pd_dev
    pd_dev_tick = {}
    reflex_state = None   # no tick field exists on the wire for RefleState —
                          # this can only ever be "last envelope-valid packet
                          # received", never "the newest one", so it is
                          # reported as such rather than as a settled state

    t0 = time.time()
    while time.time() - t0 < WINDOW_S:
        for key, _ in sel.select(timeout=0.2):
            tag = key.data
            if tag == "raw":
                data, _ = raw.recvfrom(64)
                if len(data) < struct.calcsize(RAW_FMT): continue
                f = struct.unpack_from(RAW_FMT, data)
                if f[0] != RAW_MAGIC: continue
                sid, tick = f[1], f[2]
                if sid in omega_tick and tick <= omega_tick[sid]:
                    continue  # older or reordered — proven-order sids only
                omega[sid] = f[4]
                omega_tick[sid] = tick
            elif tag == "ap":
                data, _ = ap.recvfrom(64)
                if len(data) < struct.calcsize(AP_FMT): continue
                f = struct.unpack_from(AP_FMT, data)
                if f[0] != AP_MAGIC or not f[2]: continue
                sid, tick = f[1], f[3]
                if sid in pd_dev_tick and tick <= pd_dev_tick[sid]:
                    continue
                pd_dev[sid] = f[7]
                pd_dev_tick[sid] = tick
            elif tag == "rs":
                data, _ = rs.recvfrom(64)
                if len(data) < struct.calcsize(RS_FMT): continue
                f = struct.unpack_from(RS_FMT, data)
                if f[0] != RS_MAGIC: continue
                reflex_state = f[1]

    sids = sorted(set(omega) | set(pd_dev))
    if not sids:
        print("no AxisPulse/RAW traffic seen in window — beacon(s) down?")
        sys.exit(1)

    print(f"{'sid':>4} {'omega (rad/s)':>15} {'pd_dev':>10}")
    for sid in sids:
        om = f"{omega[sid]:.6f}" if sid in omega else "?"
        pd = f"{pd_dev[sid]:.6f}" if sid in pd_dev else "?"
        print(f"{sid:>4} {om:>15} {pd:>10}")

    rname = RS_NAME[reflex_state] if reflex_state is not None else "no RefleState seen"
    print(f"\nreflex (last envelope-valid packet, unauthenticated, no ordering "
          f"proof — not a verified verdict): {rname}")


if __name__ == "__main__":
    main()
