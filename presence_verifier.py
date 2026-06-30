#!/usr/bin/env python3
"""
presence_verifier.py — Dual-probe presence verifier.

Subscribes to ProbeResult multicast (239.0.0.6:7460).
Pairs results from node 1 (Pi1) and node 2 (Pi2) by carrier epoch.
A MITM must spoof both nodes at the same epoch with identical signatures.

Verdict per epoch:
  MATCH   — cert identical, timing within tolerance
  MISMATCH — cert diverged or timing outside tolerance (MITM candidate)
  PARTIAL — only one node reported (network issue or node down)
"""
import socket, struct, time

PR_GRP   = "239.0.0.6"; PR_PORT = 7460
PR_MAGIC = 0x5050
PR_FMT   = "!HBIHHHHH8s"
PR_SIZE  = struct.calcsize(PR_FMT)

PAIR_WINDOW_S = 90    # seconds to wait for both nodes before PARTIAL verdict
TIMING_TOL_MS = 80    # ms tolerance for tcp/tls timing between nodes

NODE_NAMES = {0: "Mint", 1: "Pi1", 2: "Pi2"}

def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s

sock = _mcast_in(PR_GRP, PR_PORT)
print(f"[verifier] {PR_GRP}:{PR_PORT}  pairing Pi1+Pi2 by epoch", flush=True)

# epoch → {node_id: (result_dict, arrived_t)}
buffer: dict[int, dict] = {}

def verdict(epoch, results):
    nodes = sorted(results.keys())
    ts    = time.strftime("%H:%M:%SZ", time.gmtime())
    print(f"\n[verifier] epoch={epoch}  nodes={[NODE_NAMES.get(n,n) for n in nodes]}  {ts}", flush=True)

    if len(results) < 2:
        node = nodes[0]
        r    = results[node][0]
        print(f"  PARTIAL  only {NODE_NAMES.get(node,node)} reported"
              f"  cert={r['cert_fp']}  tls={r['tls_ms']}ms", flush=True)
        return

    # compare first two nodes
    na, nb   = nodes[0], nodes[1]
    ra, rb   = results[na][0], results[nb][0]

    flags = []
    if ra["cert_fp"] != rb["cert_fp"]:
        flags.append(f"CERT_MISMATCH  {NODE_NAMES.get(na,na)}={ra['cert_fp']}"
                     f"  {NODE_NAMES.get(nb,nb)}={rb['cert_fp']}")

    for k in ("tcp_ms", "tls_ms", "ttfb_ms"):
        diff = abs(ra[k] - rb[k])
        if diff > TIMING_TOL_MS:
            flags.append(f"{k}_DIVERGE  Δ={diff:.0f}ms"
                         f"  {NODE_NAMES.get(na,na)}={ra[k]:.0f}"
                         f"  {NODE_NAMES.get(nb,nb)}={rb[k]:.0f}")

    ent_diff = abs(ra["entropy"] - rb["entropy"])
    if ent_diff > 0.5:
        flags.append(f"ENTROPY_DIVERGE  Δ={ent_diff:.3f}")

    if flags:
        print(f"  !! MISMATCH", flush=True)
        for f in flags:
            print(f"     {f}", flush=True)
    else:
        print(f"  MATCH  cert={ra['cert_fp']}"
              f"  tcp Δ={abs(ra['tcp_ms']-rb['tcp_ms']):.0f}ms"
              f"  tls Δ={abs(ra['tls_ms']-rb['tls_ms']):.0f}ms", flush=True)
    for n in nodes:
        r = results[n][0]
        print(f"    {NODE_NAMES.get(n,n):4s}  dns={r['dns_ms']:5.0f}ms"
              f"  tcp={r['tcp_ms']:5.0f}ms  tls={r['tls_ms']:5.0f}ms"
              f"  ttfb={r['ttfb_ms']:5.0f}ms  ent={r['entropy']:.3f}", flush=True)

import selectors
sel = selectors.DefaultSelector()
sel.register(sock, selectors.EVENT_READ)

while True:
    for key, _ in sel.select(timeout=5.0):
        data, _ = sock.recvfrom(64)
        if len(data) < PR_SIZE:
            continue
        magic, node, epoch, dns, tcp, tls, ttfb, ent_x100, cert_b = \
            struct.unpack_from(PR_FMT, data)
        if magic != PR_MAGIC:
            continue
        r = dict(dns_ms=dns, tcp_ms=tcp, tls_ms=tls, ttfb_ms=ttfb,
                 entropy=ent_x100/100, cert_fp=cert_b.decode("ascii", errors="replace"))
        print(f"[verifier] rx  node={NODE_NAMES.get(node,node)}  epoch={epoch}"
              f"  cert={r['cert_fp']}  tls={tls}ms", flush=True)
        buffer.setdefault(epoch, {})[node] = (r, time.time())

    # expire unpaired epochs
    now = time.time()
    for epoch in sorted(buffer):
        results   = buffer[epoch]
        arrived_t = min(v[1] for v in results.values())
        has_both  = 1 in results and 2 in results
        expired   = (now - arrived_t) > PAIR_WINDOW_S
        if has_both or expired:
            verdict(epoch, results)
            del buffer[epoch]
            break
