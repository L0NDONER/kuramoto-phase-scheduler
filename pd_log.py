#!/usr/bin/env python3
"""
pd_log.py — full-resolution AxisPulse logger for pd(t) plotting.

Appends every AxisPulse tick (not sampled like bio.py) as CSV:
    t,tick,pd,pd_dev,locked
"""
import csv
import socket
import struct
import sys
import time

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

OUT = sys.argv[1] if len(sys.argv) > 1 else "pd_log.csv"

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", AP_PORT))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))

f = open(OUT, "a", newline="")
w = csv.writer(f)
if f.tell() == 0:
    w.writerow(["t", "tick", "pd", "pd_dev", "locked"])

print(f"[pd_log] → {OUT}", flush=True)
n = 0
while True:
    data, _ = s.recvfrom(64)
    if len(data) < AP_SIZE:
        continue
    fld = struct.unpack_from(AP_FMT, data)
    if fld[0] != AP_MAGIC:
        continue
    _, sid, locked, tick, theta1, theta2, pd, pd_dev, load_avg, drains, t0_ns = fld
    w.writerow([f"{time.time():.3f}", tick, f"{pd:.6f}", f"{pd_dev:.6f}", locked])
    n += 1
    if n % 500 == 0:
        f.flush()
        print(f"[pd_log] n={n} tick={tick} pd={pd:.4f} pd_dev={pd_dev:.5f}", flush=True)
