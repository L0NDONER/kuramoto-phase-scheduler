#!/usr/bin/env python3
"""
i0_carrier.py -- shared I(0) wire format + reader, the I(0) analog of
axis_pulse.py. AxisPulse was per-node (sid), so its readers filtered by
sid; I(0) is a single shared broadcast (see layer0_daemon.py) with no
sid concept -- every observer sees the exact same tick stream, so
next_i0_tick() has nothing to filter on.
"""
import socket
import struct

I0_GRP, I0_PORT = "239.0.0.6", 7500
I0_MAGIC = 0x4C30
I0_FMT = "!HIffd"   # magic, tick, theta, pd, t0
I0_SIZE = struct.calcsize(I0_FMT)
I0_TICK_S = 0.05    # 20Hz


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def next_i0_tick(sel, i0_in, timeout=1.0):
    """One select+recv pass; returns the observed tick, or None."""
    for key, _ in sel.select(timeout=timeout):
        data, _ = i0_in.recvfrom(64)
        if len(data) < I0_SIZE:
            continue
        magic, tick, theta, pd, t0 = struct.unpack_from(I0_FMT, data)
        if magic != I0_MAGIC:
            continue
        return tick
    return None


def bytes_to_bits(data: bytes):
    """MSB-first bit sequence, one bit per pulse slot. Same convention
    as axis_pulse.py's hash_to_bits(), generalized to any bytes, not
    just a hash digest -- the ticket text itself is the payload here,
    not a proof-of-knowledge hash of it."""
    return [(byte >> i) & 1 for byte in data for i in range(7, -1, -1)]


def bits_to_bytes(bits):
    """Inverse of bytes_to_bits(). len(bits) must be a multiple of 8."""
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)
