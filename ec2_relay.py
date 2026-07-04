#!/usr/bin/env python3
"""
ec2_relay.py — relay AxisPulse + phase-auth challenge multicast to EC2
over the WireGuard tunnel as unicast, so EC2's phase_auth_prover.py can
observe the real quartz signal and answer challenges.

Response traffic needs no relay: the prover replies directly to the
challenger's source (Mint's WG address), which phase_auth.py already
listens for.
"""
import socket, struct, selectors

AP_GRP   = "239.0.0.2"; AP_PORT  = 7404
PA_GRP   = "239.0.0.5"; PA_CHAL_PORT = 7451

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

ap_in   = mcast_in(AP_GRP, AP_PORT)
chal_in = mcast_in(PA_GRP, PA_CHAL_PORT)
out     = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

sel = selectors.DefaultSelector()
sel.register(ap_in,   selectors.EVENT_READ, data=("ap", AP_PORT))
sel.register(chal_in, selectors.EVENT_READ, data=("chal", PA_CHAL_PORT))

print(f"[relay] {AP_GRP}:{AP_PORT} + {PA_GRP}:{PA_CHAL_PORT} → {EC2_WG_IP} (unicast)", flush=True)

n_ap = n_chal = 0
while True:
    for key, _ in sel.select(timeout=1.0):
        tag, port = key.data
        data, _ = key.fileobj.recvfrom(128)
        out.sendto(data, (EC2_WG_IP, port))
        if tag == "ap":
            n_ap += 1
            if n_ap % 500 == 0:
                print(f"[relay] ap={n_ap} chal={n_chal}", flush=True)
        else:
            n_chal += 1
            print(f"[relay] challenge relayed → {EC2_WG_IP}:{port}", flush=True)
