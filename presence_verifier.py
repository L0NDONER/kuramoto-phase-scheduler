#!/usr/bin/env python3
"""
presence_verifier.py — Multi-node presence verifier.

Pairs ProbeResult packets from Pi1 (LAN), Pi2 (LAN), EC2 (WireGuard).
Majority vote on cert + ASN. Outlier identification.

  Pi1 + Pi2 agree, EC2 outlier  → your LAN is clean, EC2 path differs
  EC2 + Pi2 agree, Pi1 outlier  → Pi1 compromised
  EC2 + Pi1 agree, Pi2 outlier  → Pi2 compromised
  Pi1 + Pi2 agree, EC2 missing  → EC2 anchor down (blind spot warning)
  All three agree                → MATCH
  All three disagree             → full MISMATCH

Receives:
  Pi1, Pi2 → multicast  239.0.0.6:7460
  EC2      → unicast    <mint-wg-ip>:7460
"""
import selectors, socket, struct, time

PR_GRP   = "239.0.0.6"; PR_PORT = 7460
PR_MAGIC = 0x5050
PR_FMT   = "!HBIHHHHH8s4s"
PR_SIZE  = struct.calcsize(PR_FMT)

PAIR_WINDOW_S    = 90
TIMING_TOL_MS    = 80
EC2_TIMING_SKIP  = True   # EC2 path is WAN — timing diverge vs LAN is expected, skip those pairs
NODE_NAMES    = {0: "Mint", 1: "Pi1", 2: "Pi2", 3: "EC2"}


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def _name(n):
    return NODE_NAMES.get(n, str(n))


def _same_asn(a, b):
    return a.split(".")[0] == b.split(".")[0]


def verdict(epoch, results):
    nodes = sorted(results.keys())
    ts    = time.strftime("%H:%M:%SZ", time.gmtime())
    names = [_name(n) for n in nodes]
    print(f"\n[verifier] epoch={epoch}  nodes={names}  {ts}", flush=True)

    if len(results) == 1:
        n = nodes[0]; r = results[n][0]
        print(f"  PARTIAL  only {_name(n)} reported"
              f"  cert={r['cert_fp']}  ip={r['ip']}", flush=True)
        return

    # Collect all pairs and find flags
    flags   = []
    outlier = {}   # node → count of disagreements

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            na, nb = nodes[i], nodes[j]
            ra, rb = results[na][0], results[nb][0]

            if ra["cert_fp"] != rb["cert_fp"]:
                flags.append(f"CERT_MISMATCH  {_name(na)}={ra['cert_fp']}"
                             f"  {_name(nb)}={rb['cert_fp']}")
                outlier[na] = outlier.get(na, 0) + 1
                outlier[nb] = outlier.get(nb, 0) + 1

            if not _same_asn(ra["ip"], rb["ip"]):
                flags.append(f"ASN_MISMATCH  {_name(na)}={ra['ip']}"
                             f"  {_name(nb)}={rb['ip']}")
                outlier[na] = outlier.get(na, 0) + 1
                outlier[nb] = outlier.get(nb, 0) + 1

            if not (EC2_TIMING_SKIP and 3 in (na, nb)):
                for k in ("tcp_ms", "tls_ms"):
                    d = abs(ra[k] - rb[k])
                    if d > TIMING_TOL_MS:
                        flags.append(f"{k}_DIVERGE  {_name(na)}={ra[k]:.0f}"
                                     f"  {_name(nb)}={rb[k]:.0f}  Δ={d:.0f}ms")

    # Identify outlier (node that disagrees with everyone else)
    if outlier and len(nodes) >= 3:
        worst = max(outlier, key=outlier.get)
        if outlier[worst] == len(nodes) - 1:
            flags.append(f"OUTLIER  {_name(worst)} disagrees with all others"
                         f" — {_name(worst)} path may be intercepted")

    # EC2 missing warning
    if 3 not in results:
        flags.append("EC2_ANCHOR_MISSING  router-level poison undetectable")

    if flags:
        print(f"  !! MISMATCH", flush=True)
        for fl in flags:
            print(f"     {fl}", flush=True)
    else:
        r0 = results[nodes[0]][0]
        print(f"  MATCH  cert={r0['cert_fp']}  {len(nodes)}/3 nodes agree", flush=True)

    for n in nodes:
        r = results[n][0]
        print(f"    {_name(n):4s}  ip={r['ip']:15s}"
              f"  tcp={r['tcp_ms']:4.0f}ms  tls={r['tls_ms']:4.0f}ms"
              f"  ttfb={r['ttfb_ms']:4.0f}ms  ent={r['entropy']:.3f}"
              f"  cert={r['cert_fp']}", flush=True)


sock = _mcast_in(PR_GRP, PR_PORT)
print(f"[verifier] :{PR_PORT}  Pi1+Pi2 (mcast) + EC2 (unicast)  majority-vote", flush=True)

buffer = {}
sel    = selectors.DefaultSelector()
sel.register(sock, selectors.EVENT_READ)

while True:
    for key, _ in sel.select(timeout=5.0):
        data, addr = sock.recvfrom(128)
        if len(data) < PR_SIZE:
            continue
        magic, node, epoch, dns, tcp, tls, ttfb, ent_x100, cert_b, ip_b = \
            struct.unpack_from(PR_FMT, data)
        if magic != PR_MAGIC:
            continue
        ip = socket.inet_ntoa(ip_b)
        r  = dict(dns_ms=dns, tcp_ms=tcp, tls_ms=tls, ttfb_ms=ttfb,
                  entropy=ent_x100 / 100,
                  cert_fp=cert_b.decode("ascii", errors="replace"),
                  ip=ip)
        # Snap epoch to nearest existing bucket within ±1 to handle NTP skew
        target = epoch
        for e in (epoch - 1, epoch + 1):
            if e in buffer and node not in buffer[e]:
                target = e
                break
        print(f"[verifier] rx  {_name(node):4s}  epoch={epoch}→{target}"
              f"  ip={ip}  cert={r['cert_fp']}  tls={tls}ms", flush=True)
        buffer.setdefault(target, {})[node] = (r, time.time())

    now = time.time()
    for epoch in sorted(buffer):
        results   = buffer[epoch]
        arrived_t = min(v[1] for v in results.values())
        has_lan   = 1 in results and 2 in results
        expired   = (now - arrived_t) > PAIR_WINDOW_S
        if (has_lan and (3 in results or now - arrived_t > 60)) or expired:
            verdict(epoch, results)
            del buffer[epoch]
            break
