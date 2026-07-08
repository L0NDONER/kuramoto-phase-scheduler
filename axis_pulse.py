#!/usr/bin/env python3
"""
axis_pulse.py — shared AxisPulse wire format + reader for the carrier
senders (nazare_carrier_sender.py, fake_carrier_sender.py) and the
hash-to-bits pulse-coding step used across all of them
(+ pulse_hash_send.py, adversary_sender.py).
"""
import socket
import struct

AP_GRP, AP_PORT = "239.0.0.2", 7404
AP_FMT = ">HBBIfffffHQ"
AP_MAGIC = 0x4158
AP_TICK_S = 0.01   # quartz_beacon.py TICK_S — fixed cadence of the carrier


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def next_ap_tick(sel, ap_in, sid, timeout=1.0):
    """One select+recv pass; returns the observed tick for this sid, or None."""
    for key, _ in sel.select(timeout=timeout):
        data, _ = ap_in.recvfrom(64)
        if len(data) < struct.calcsize(AP_FMT):
            continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] != AP_MAGIC or f[1] != sid:
            continue
        return f[3]
    return None


def hash_to_bits(digest):
    """MSB-first bit sequence of a hash digest, one bit per pulse slot."""
    return [(byte >> i) & 1 for byte in digest for i in range(7, -1, -1)]
