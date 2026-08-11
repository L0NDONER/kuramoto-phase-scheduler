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
[Update: layer1/run_layer1.py is that build -- listens on this same wire,
RK4-integrates its own carrier toward the mean-field psi instead of just
snapshotting it. fractal_layer1/layer1_aggregator.py is still the
snapshot-only version; both are legitimate, different jobs.]

dt_lag_s is the sender's own phase-tracking error converted to seconds
(tracking_err / omega -- how far in time this node's carrier currently
lags(+)/leads(-) whatever target it's forced toward). Only meaningful for
a sender that's actually forced toward a single tracked target (Layer 1,
or any future single-oscillator forced layer) -- a Layer 0 node's r/psi
is a mean field over N of its own oscillators, not one phase with one
well-defined tracking error, so Layer 0 senders leave this NaN (see
send_report's default) rather than reporting a fabricated number.
Receivers that don't care can ignore it same as they'd ignore any other
field; receivers that do care must check math.isnan() before using it.
"""
import socket
import struct

GRP = "239.0.0.6"
PORT = 7460
MAGIC = 0x4C30   # "L0"
FMT = "!HBfff"   # magic, node_id, r, psi, dt_lag_s
SIZE = struct.calcsize(FMT)

# Fixed small registry, not a general discovery protocol -- matches this
# session's node count (real machines + one aggregate), extend by hand if
# more show up. "layer1" is Layer 1 itself broadcasting r1/psi1 back onto
# the same wire format/port a Layer 0 node uses -- the recursive structure
# continuing one level up, not a special case: whatever subscribes to a
# Layer 0 report can subscribe to Layer 1's the same way.
NODE_IDS = {"mint": 1, "pi2": 2, "pi1": 3, "gpu": 4, "layer1": 5, "market": 6}
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


def send_report(sock, node_name, r, psi, dt_lag_s=float("nan")):
    node_id = NODE_IDS[node_name]
    pkt = struct.pack(FMT, MAGIC, node_id, r, psi, dt_lag_s)
    sock.sendto(pkt, (GRP, PORT))


def parse_report(data):
    """Returns (node_name, r, psi, dt_lag_s) or None if not a valid
    report packet. dt_lag_s is NaN for senders that don't track a single
    target (see module docstring) -- check math.isnan() before using it."""
    if len(data) < SIZE:
        return None
    magic, node_id, r, psi, dt_lag_s = struct.unpack_from(FMT, data)
    if magic != MAGIC:
        return None
    name = NODE_NAMES.get(node_id)
    if name is None:
        return None
    return name, r, psi, dt_lag_s
