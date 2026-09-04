#!/usr/bin/env python3
"""
i0_relay.py -- relay I(0) + phase-auth challenge multicast to EC2 over
the WireGuard tunnel as unicast, so EC2's i0_phase_auth_prover.py can
observe the real Layer0 substrate and answer challenges. Same pattern
as ec2_relay.py (AxisPulse version, now retired along with quartz),
just carrying I(0)'s wire instead.

Response traffic needs no relay: the prover replies directly to the
challenger's source (Mint's WG address), which i0_phase_auth.py
already listens for.
"""
import socket, struct, selectors

I0_GRP, I0_PORT = "239.0.0.6", 7500
PA_GRP, PA_CHAL_PORT = "239.0.0.5", 7451

EC2_WG_IP = "10.8.0.1"

def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s

i0_in   = mcast_in(I0_GRP, I0_PORT)
chal_in = mcast_in(PA_GRP, PA_CHAL_PORT)
out     = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

sel = selectors.DefaultSelector()
sel.register(i0_in,   selectors.EVENT_READ, data=("i0", I0_PORT))
sel.register(chal_in, selectors.EVENT_READ, data=("chal", PA_CHAL_PORT))

print(f"[relay] {I0_GRP}:{I0_PORT} + {PA_GRP}:{PA_CHAL_PORT} -> {EC2_WG_IP} (unicast)", flush=True)

n_i0 = n_chal = 0
while True:
    for key, _ in sel.select(timeout=1.0):
        tag, port = key.data
        data, _ = key.fileobj.recvfrom(128)
        out.sendto(data, (EC2_WG_IP, port))
        if tag == "i0":
            n_i0 += 1
            if n_i0 % 100 == 0:
                print(f"[relay] i0={n_i0} chal={n_chal}", flush=True)
        else:
            n_chal += 1
            print(f"[relay] challenge relayed -> {EC2_WG_IP}:{port}", flush=True)
