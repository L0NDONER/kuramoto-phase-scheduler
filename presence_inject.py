#!/usr/bin/env python3
"""
presence_inject.py — Read PROBE: JSON lines from stdin (EC2 via SSH pipe),
pack into ProbeResult struct, send to local verifier on 127.0.0.1:7460.

Usage: ssh aws python3 ~/presence_probe.py amazon.co.uk 443 3 --stdout | python3 presence_inject.py
"""
import json, socket, struct, sys

PR_PORT  = 7460
PR_FMT   = "!HBIHHHHH8s4s"
PR_MAGIC = 0x5050

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
print("[inject] ready — forwarding EC2 probe results to 127.0.0.1:7460", flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("PROBE:"):
        print(line, flush=True)   # pass through log lines
        continue
    try:
        d = json.loads(line[6:])
        pkt = struct.pack(PR_FMT, PR_MAGIC, d["node"], d["epoch"],
                          d["dns_ms"], d["tcp_ms"], d["tls_ms"], d["ttfb_ms"],
                          d["ent_x100"],
                          d["cert_fp"][:8].encode(),
                          socket.inet_aton(d["ip"]))
        sock.sendto(pkt, ("127.0.0.1", PR_PORT))
        print(f"[inject] EC2  epoch={d['epoch']}  ip={d['ip']}  cert={d['cert_fp']}", flush=True)
    except Exception as e:
        print(f"[inject] ERR  {e}  line={line!r}", flush=True)
