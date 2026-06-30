#!/usr/bin/env python3
"""
presence_probe.py — Physical signature prober.

Standalone — no carrier, no beacon. Probes every PROBE_INTERVAL seconds.
Measures: dns_ms, tcp_ms, tls_ms, ttfb_ms, entropy, cert_fp.

ASN validation via Team Cymru DNS TXT (no API key needed).
Cross-node comparison via ProbeResult multicast 239.0.0.6:7460.
EC2 anchor (node=3) sends unicast to Mint WireGuard IP instead.

Usage: python3 presence_probe.py [host] [port] [node_id] [dest_ip]
  node_id: 1=Pi1  2=Pi2  0=Mint  3=EC2   (default 1)
  dest_ip: unicast destination instead of multicast (EC2 use only)
"""
import collections, hashlib, math, socket, ssl, struct, subprocess, sys, time

PR_GRP   = "239.0.0.6"; PR_PORT = 7460
PR_FMT   = "!HBIHHHHH8s4s"
PR_MAGIC = 0x5050

# Known ASNs per hostname — resolved IP must belong to one of these
KNOWN_ASNS = {
    "amazon.co.uk":  {16509},
    "amazon.com":    {16509},
    "amazon.de":     {16509},
    "amazon.fr":     {16509},
    "bbc.co.uk":     {2818, 20940},   # BBC + Akamai
    "bbc.com":       {2818, 20940},
    "paypal.com":    {17012},
    "paypal.co.uk":  {17012},
    "google.com":    {15169},
    "youtube.com":   {15169},
    "facebook.com":  {32934},
    "apple.com":     {714},
    "twitter.com":   {13414},
}


def _lookup_asn(ip):
    """Team Cymru DNS TXT ASN lookup. Returns int or None if unavailable."""
    rev = ".".join(reversed(ip.split(".")))
    try:
        out = subprocess.check_output(
            ["dig", "+short", "TXT", f"{rev}.origin.asn.cymru.com", "@1.1.1.1"],
            timeout=5, stderr=subprocess.DEVNULL
        ).decode()
        for line in out.strip().splitlines():
            line = line.strip('" ')
            if "|" in line:
                return int(line.split("|")[0].strip())
    except Exception:
        pass
    return None


def _asn_ok(ip, host, asn):
    expected = KNOWN_ASNS.get(host)
    if not expected:
        return True, None
    if asn is None:
        return True, None   # ASN lookup failed — skip, don't block
    if asn not in expected:
        return False, asn
    return True, asn


HOST           = sys.argv[1] if len(sys.argv) > 1 else "amazon.co.uk"
PORT           = int(sys.argv[2]) if len(sys.argv) > 2 else 443
NODE_ID        = int(sys.argv[3]) if len(sys.argv) > 3 else 1
DEST_IP        = sys.argv[4] if len(sys.argv) > 4 else None   # unicast for EC2
STDOUT_MODE    = "--stdout" in sys.argv                        # pipe result as JSON, no UDP
PROBE_INTERVAL = 75
WARMUP         = 5
EMA_A          = 0.25
ALERT_SD       = 2.5


def entropy(data):
    if not data:
        return 0.0
    c = collections.Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def probe(host, port):
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])

    t0     = time.perf_counter()
    addrs  = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    dns_ms = (time.perf_counter() - t0) * 1000
    ip     = addrs[0][4][0]

    asn = _lookup_asn(ip)
    ok, bad_asn = _asn_ok(ip, host, asn)
    if not ok:
        raise ValueError(f"ASN_MISMATCH  {host} → {ip}  ASN={bad_asn}  expected={KNOWN_ASNS[host]}")

    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(10)
    t1     = time.perf_counter()
    raw.connect((ip, port))
    tcp_ms = (time.perf_counter() - t1) * 1000

    t2     = time.perf_counter()
    ssock  = ctx.wrap_socket(raw, server_hostname=host)
    tls_ms = (time.perf_counter() - t2) * 1000

    cert_der = ssock.getpeercert(binary_form=True)
    cert_fp  = hashlib.sha256(cert_der).hexdigest()[:16]

    ssock.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    t3      = time.perf_counter()
    body    = b""
    ttfb_ms = None
    while True:
        chunk = ssock.recv(4096)
        if not chunk:
            break
        if ttfb_ms is None:
            ttfb_ms = (time.perf_counter() - t3) * 1000
        body += chunk
    ssock.close()

    return dict(dns_ms=dns_ms, tcp_ms=tcp_ms, tls_ms=tls_ms,
                ttfb_ms=ttfb_ms or 0.0, entropy=entropy(body[:2048]),
                cert_fp=cert_fp, ip=ip, asn=asn)


_ema = {}; _ema_sq = {}; _cert_canonical = None
METRICS = ["dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "entropy"]

def _update(k, v):
    if k not in _ema:
        _ema[k] = v; _ema_sq[k] = v * v
    else:
        _ema[k]    = EMA_A * v    + (1 - EMA_A) * _ema[k]
        _ema_sq[k] = EMA_A * v**2 + (1 - EMA_A) * _ema_sq[k]

def _sd(k):
    return math.sqrt(max(0.0, _ema_sq[k] - _ema[k] ** 2))


if not STDOUT_MODE:
    pr_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if not DEST_IP:
        pr_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
transport = "stdout/ssh" if STDOUT_MODE else (f"unicast→{DEST_IP}" if DEST_IP else f"mcast→{PR_GRP}")
print(f"[presence] node={NODE_ID}  {HOST}:{PORT}  every {PROBE_INTERVAL}s  {transport}", flush=True)

probe_count = 0

while True:
    epoch = int(time.time()) // PROBE_INTERVAL   # shared wall-clock epoch — same on all nodes
    try:
        r = probe(HOST, PORT)
    except Exception as e:
        print(f"[presence] FAIL  {e}", flush=True)
        next_epoch_t = (int(time.time()) // PROBE_INTERVAL + 1) * PROBE_INTERVAL
        time.sleep(max(1, next_epoch_t - time.time()))
        continue

    probe_count += 1
    for k in METRICS:
        _update(k, r[k])

    flags = []
    if probe_count > WARMUP:
        if _cert_canonical and r["cert_fp"] != _cert_canonical:
            flags.append(f"CERT_CHANGED {_cert_canonical}→{r['cert_fp']}")
        for k in METRICS:
            sd = _sd(k)
            if sd > 0:
                z = abs(r[k] - _ema[k]) / sd
                if z > ALERT_SD:
                    flags.append(f"{k}={r[k]:.1f} z={z:.1f}σ μ={_ema[k]:.1f}")
    else:
        if _cert_canonical is None:
            _cert_canonical = r["cert_fp"]

    tag = "BUILDING" if probe_count <= WARMUP else ("ALERT  " if flags else "MATCH  ")
    asn_str = str(r["asn"]) if r["asn"] else "?"
    print(f"[presence] epoch={epoch}  #{probe_count:3d}  {tag}", flush=True)
    print(f"           ip={r['ip']}  ASN={asn_str}  dns={r['dns_ms']:.0f}ms"
          f"  tcp={r['tcp_ms']:.0f}ms  tls={r['tls_ms']:.0f}ms"
          f"  ttfb={r['ttfb_ms']:.0f}ms  ent={r['entropy']:.3f}"
          f"  cert={r['cert_fp']}", flush=True)
    for fl in flags:
        print(f"           !! {fl}", flush=True)

    if STDOUT_MODE:
        import json
        print("PROBE:" + json.dumps(dict(
            node=NODE_ID, epoch=epoch,
            dns_ms=int(r["dns_ms"]), tcp_ms=int(r["tcp_ms"]),
            tls_ms=int(r["tls_ms"]), ttfb_ms=int(r["ttfb_ms"]),
            ent_x100=int(r["entropy"] * 100),
            cert_fp=r["cert_fp"][:8], ip=r["ip"]
        )), flush=True)
    else:
        try:
            pkt = struct.pack(PR_FMT, PR_MAGIC, NODE_ID, epoch,
                              int(r["dns_ms"]), int(r["tcp_ms"]),
                              int(r["tls_ms"]), int(r["ttfb_ms"]),
                              int(r["entropy"] * 100),
                              r["cert_fp"][:8].encode(),
                              socket.inet_aton(r["ip"]))
            dest = (DEST_IP, PR_PORT) if DEST_IP else (PR_GRP, PR_PORT)
            pr_out.sendto(pkt, dest)
        except OSError:
            pass

    time.sleep(PROBE_INTERVAL)
