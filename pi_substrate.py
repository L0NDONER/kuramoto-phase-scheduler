#!/usr/bin/env python3
"""
pi_substrate.py — Monte Carlo π driven by the Kuramoto substrate.

Each locked AxisPulse tick contributes one sample: (sin θ₁, sin θ₂).
Points inside the unit circle → π ≈ 4 × hits / total.

The samples are not independent (θ₁, θ₂ are coupled oscillators) so
convergence is substrate-shaped, not purely statistical. The bias IS
the geometry of the carrier.

Run: python3 pi_substrate.py
"""
import socket, struct, math

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"
AP_SIZE = struct.calcsize(AP_FMT)
AP_MAGIC = 0x4158

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
s.bind(("", AP_PORT))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))

hits = 0; total = 0
prev_theta = None
REPORT = 500

print(f"π from substrate  Leibniz series, substrate-paced", flush=True)
print(f"π/4 = 1 − 1/3 + 1/5 − 1/7 + …  one term per locked tick", flush=True)
print(f"Reporting every {REPORT} ticks — Ctrl+C to stop\n", flush=True)

acc = 0.0   # running Leibniz sum

while True:
    data, _ = s.recvfrom(64)
    if len(data) < AP_SIZE:
        continue
    f = struct.unpack_from(AP_FMT, data)
    if f[0] != AP_MAGIC or not f[2]:
        continue

    sign = 1 if total % 2 == 0 else -1
    acc  += sign / (2 * total + 1)
    total += 1
    pi_est = 4.0 * acc

    if total % REPORT == 0:
        err = pi_est - math.pi
        print(f"  n={total:8d}  π ≈ {pi_est:.6f}  err={err:+.6f}", flush=True)
