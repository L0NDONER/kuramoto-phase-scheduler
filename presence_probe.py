#!/usr/bin/env python3
"""
presence_probe.py — Physical signature prober.

Standalone — no carrier, no beacon, no AxisPulse dependency.
Probes a target every PROBE_INTERVAL seconds. Measures:
  dns_ms   — DNS resolution time
  tcp_ms   — TCP connect time
  tls_ms   — TLS handshake duration
  ttfb_ms  — time to first byte
  entropy  — Shannon entropy of first 2KB of response body
  cert_fp  — SHA256[:16] of leaf certificate DER

Validates resolved IP against known ASN ranges per hostname.
Builds EMA canonical profile after WARMUP probes; flags deviations.
Emits ProbeResult on 239.0.0.6:7460 for presence_verifier.py.

Usage: python3 presence_probe.py [host] [port] [node_id]
       node_id: 1=Pi1  2=Pi2  0=Mint  (default 1)
       Default: amazon.co.uk 443 1
"""
import collections, hashlib, math, socket, ssl, struct, sys, time

# ProbeResult multicast — magic(H) node(B) epoch(I) dns(H) tcp(H) tls(H) ttfb(H) ent_x100(H) cert_fp(8s) ip(4s)
PR_GRP   = "239.0.0.6"; PR_PORT = 7460
PR_FMT   = "!HBIHHHHH8s4s"
PR_MAGIC = 0x5050   # "PP"

# Known ASN ranges per hostname — (network_int, mask_int)
def _cidr(net, bits):
    n = struct.unpack("!I", socket.inet_aton(net))[0]
    m = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF
    return (n & m, m)

_AWS = [_cidr("3.0.0.0", 8), _cidr("13.0.0.0", 8), _cidr("18.0.0.0", 8),
        _cidr("52.0.0.0", 8), _cidr("54.0.0.0", 8), _cidr("99.77.0.0", 16),
        _cidr("130.176.0.0", 16), _cidr("143.204.0.0", 16), _cidr("205.251.192.0", 19)]

KNOWN_RANGES = {
    "amazon.co.uk": _AWS,
    "amazon.com":   _AWS,
    "amazon.de":    _AWS,
    "amazon.fr":    _AWS,
}

def _ip_ok(ip_str, host):
    ranges = KNOWN_RANGES.get(host)
    if not ranges:
        return True
    ip_int = struct.unpack("!I", socket.inet_aton(ip_str))[0]
    return any((ip_int & m) == n for n, m in ranges)

HOST            = sys.argv[1] if len(sys.argv) > 1 else "amazon.co.uk"
PORT            = int(sys.argv[2]) if len(sys.argv) > 2 else 443
NODE_ID         = int(sys.argv[3]) if len(sys.argv) > 3 else 1
PROBE_INTERVAL  = 75      # seconds between probes
WARMUP          = 5
EMA_A           = 0.25
ALERT_SD        = 2.5


def entropy(data):
    if not data:
        return 0.0
    c = collections.Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def probe(host, port):
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])

    t0    = time.perf_counter()
    addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    dns_ms = (time.perf_counter() - t0) * 1000
    ip     = addrs[0][4][0]

    if not _ip_ok(ip, host):
        raise ValueError(f"DNS_POISON  {host} → {ip} not in known ranges")

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
                cert_fp=cert_fp, ip=ip)


# EMA canonical
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


pr_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
pr_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

print(f"[presence] node={NODE_ID}  {HOST}:{PORT}  every {PROBE_INTERVAL}s", flush=True)

epoch       = 0
probe_count = 0

while True:
    try:
        r = probe(HOST, PORT)
    except Exception as e:
        print(f"[presence] FAIL  {e}", flush=True)
        time.sleep(PROBE_INTERVAL)
        epoch += 1
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
    print(f"[presence] epoch={epoch}  #{probe_count:3d}  {tag}", flush=True)
    print(f"           ip={r['ip']}  dns={r['dns_ms']:.0f}ms  tcp={r['tcp_ms']:.0f}ms"
          f"  tls={r['tls_ms']:.0f}ms  ttfb={r['ttfb_ms']:.0f}ms"
          f"  ent={r['entropy']:.3f}  cert={r['cert_fp']}", flush=True)
    for fl in flags:
        print(f"           !! {fl}", flush=True)

    try:
        pkt = struct.pack(PR_FMT, PR_MAGIC, NODE_ID, epoch,
                          int(r["dns_ms"]), int(r["tcp_ms"]),
                          int(r["tls_ms"]), int(r["ttfb_ms"]),
                          int(r["entropy"] * 100),
                          r["cert_fp"][:8].encode(),
                          socket.inet_aton(r["ip"]))
        pr_out.sendto(pkt, (PR_GRP, PR_PORT))
    except OSError:
        pass

    epoch += 1
    time.sleep(PROBE_INTERVAL)
