"""layer0_report.py — wire format for a Layer 0 node reporting its
order parameter (r, psi) up to a Layer 1 aggregator.

Multicast, same convention as the rest of the substrate (AxisPulse on
239.0.0.2:7404, RefleState on 239.0.0.4:7450, etc.) -- next unused
group/port pair: 239.0.0.6:7460.

This is deliberately NOT itself a phase-integrated oscillator layer --
Layer 1 just takes an instantaneous mean-field snapshot of whatever
psi values arrive (r1 = |mean(e^(i*psi_i))|), it doesn't RK4-integrate
its own carrier. Honest about that: this is the recursive mean-field
idea demonstrated in its simplest working form, not a second full
Layer0Node-style oscillator stacked on top. Build that properly later
if the simple version isn't enough, don't pretend it's already there.
"""
import socket
import struct

GRP = "239.0.0.6"
PORT = 7460
MAGIC = 0x4C30   # "L0"
FMT = "!HBff"    # magic, node_id, r, psi
SIZE = struct.calcsize(FMT)

# Fixed small registry, not a general discovery protocol -- matches this
# session's node count (2-3 real machines), extend by hand if more show up.
NODE_IDS = {"mint": 1, "pi2": 2, "pi1": 3, "gpu": 4}
NODE_NAMES = {v: k for k, v in NODE_IDS.items()}


def mcast_out(ttl=8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    return s


def mcast_in():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(GRP) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def send_report(sock, node_name, r, psi):
    node_id = NODE_IDS[node_name]
    pkt = struct.pack(FMT, MAGIC, node_id, r, psi)
    sock.sendto(pkt, (GRP, PORT))


def parse_report(data):
    """Returns (node_name, r, psi) or None if not a valid report packet."""
    if len(data) < SIZE:
        return None
    magic, node_id, r, psi = struct.unpack_from(FMT, data)
    if magic != MAGIC:
        return None
    name = NODE_NAMES.get(node_id)
    if name is None:
        return None
    return name, r, psi
