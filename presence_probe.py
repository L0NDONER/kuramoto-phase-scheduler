#!/usr/bin/env python3
"""
presence_probe.py — Physical signature prober.

Probes a target at each PROBE_EVERY locked AxisPulse ticks. Measures:
  dns_ms    — DNS resolution time
  tcp_ms    — TCP connect time
  tls_ms    — TLS handshake duration
  ttfb_ms   — time to first byte
  entropy   — Shannon entropy of first 2KB of response body
  cert_fp   — SHA256[:16] of leaf certificate DER
  cipher    — negotiated cipher suite

Builds an EMA canonical profile after WARMUP probes.
Flags any metric that deviates > ALERT_SD standard deviations.

Emits ProbeResult on 239.0.0.6:7460 so a verifier can compare
results from multiple nodes (Pi1 + Pi2) side by side.

Usage: python3 presence_probe.py [host] [port]
       Default: amazon.co.uk 443
"""
import collections, hashlib, math, socket, ssl, struct, sys, time

# AxisPulse
AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

# ProbeResult multicast
PR_GRP  = "239.0.0.6"; PR_PORT = 7460
# magic(H) node_id(B) tick(I) dns_ms(H) tcp_ms(H) tls_ms(H) ttfb_ms(H) entropy_x100(H) cert_fp(8s) — 22 bytes
PR_FMT  = "!HBIHHHHHHHHHHHHHHHe8s"   # simplified below
PR_MAGIC = 0x5050   # "PP"

HOST        = sys.argv[1] if len(sys.argv) > 1 else "amazon.co.uk"
PORT        = int(sys.argv[2]) if len(sys.argv) > 2 else 443
NODE_ID     = int(sys.argv[3]) if len(sys.argv) > 3 else 1  # 1=Pi1 2=Pi2 0=Mint
PROBE_EVERY = 3000   # locked ticks between probes (~75s at 40Hz)
WARMUP      = 5      # probes before canonical is live
EMA_A       = 0.25
ALERT_SD    = 2.5    # sigma threshold for alert


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    c = collections.Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def probe(host: str, port: int) -> dict:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])

    # DNS
    t0 = time.perf_counter()
    addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    dns_ms = (time.perf_counter() - t0) * 1000
    ip = addrs[0][4][0]

    # TCP connect
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(10)
    t1 = time.perf_counter()
    raw.connect((ip, port))
    tcp_ms = (time.perf_counter() - t1) * 1000

    # TLS handshake
    t2 = time.perf_counter()
    ssock = ctx.wrap_socket(raw, server_hostname=host)
    tls_ms = (time.perf_counter() - t2) * 1000

    cert_der = ssock.getpeercert(binary_form=True)
    cert_fp  = hashlib.sha256(cert_der).hexdigest()[:16]
    cipher   = ssock.cipher()[0]
    tls_ver  = ssock.version()

    # TTFB
    ssock.sendall(
        f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
    )
    t3    = time.perf_counter()
    body  = b""
    ttfb_ms = None
    while True:
        chunk = ssock.recv(4096)
        if not chunk:
            break
        if ttfb_ms is None:
            ttfb_ms = (time.perf_counter() - t3) * 1000
        body += chunk
    ssock.close()

    ent = entropy(body[:2048])

    return dict(
        dns_ms=dns_ms, tcp_ms=tcp_ms, tls_ms=tls_ms,
        ttfb_ms=ttfb_ms or 0.0, entropy=ent,
        cert_fp=cert_fp, cipher=cipher, tls_ver=tls_ver,
        ip=ip, body_len=len(body),
    )


# EMA canonical state
_ema:    dict[str, float] = {}
_ema_sq: dict[str, float] = {}
_cert_canonical: str | None = None

METRICS = ["dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "entropy"]


def _update(key: str, val: float):
    if key not in _ema:
        _ema[key] = val; _ema_sq[key] = val * val
    else:
        _ema[key]    = EMA_A * val    + (1 - EMA_A) * _ema[key]
        _ema_sq[key] = EMA_A * val**2 + (1 - EMA_A) * _ema_sq[key]


def _sd(key: str) -> float:
    return math.sqrt(max(0.0, _ema_sq[key] - _ema[key] ** 2))


# AxisPulse socket
def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    return s


ap_sock = _mcast_in(AP_GRP, AP_PORT)

pr_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
pr_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

print(f"[presence] node={NODE_ID}  target={HOST}:{PORT}  interval={PROBE_EVERY} ticks", flush=True)

probe_count  = 0
last_probe_t = 0

while True:
    data, _ = ap_sock.recvfrom(64)
    if len(data) < AP_SIZE:
        continue
    f = struct.unpack_from(AP_FMT, data)
    if f[0] != AP_MAGIC or not f[2]:
        continue
    tick = f[3]

    if tick - last_probe_t < PROBE_EVERY:
        continue
    last_probe_t = tick

    try:
        r = probe(HOST, PORT)
    except Exception as e:
        print(f"[presence] tick={tick}  FAIL  {e}", flush=True)
        continue

    probe_count += 1
    for k in METRICS:
        _update(k, r[k])

    flags = []
    if probe_count > WARMUP:
        # cert change is always an alert
        if _cert_canonical and r["cert_fp"] != _cert_canonical:
            flags.append(f"CERT_CHANGED({_cert_canonical}→{r['cert_fp']})")
        for k in METRICS:
            sd = _sd(k)
            if sd > 0:
                z = abs(r[k] - _ema[k]) / sd
                if z > ALERT_SD:
                    flags.append(f"{k}={r[k]:.1f}(z={z:.1f}σ,μ={_ema[k]:.1f})")
    else:
        if _cert_canonical is None:
            _cert_canonical = r["cert_fp"]

    tag = "BUILDING" if probe_count <= WARMUP else ("ALERT  " if flags else "MATCH  ")

    print(f"[presence] tick={tick}  #{probe_count:3d}  {tag}", flush=True)
    print(f"           dns={r['dns_ms']:6.1f}ms  tcp={r['tcp_ms']:6.1f}ms"
          f"  tls={r['tls_ms']:6.1f}ms  ttfb={r['ttfb_ms']:6.1f}ms"
          f"  ent={r['entropy']:.3f}  cert={r['cert_fp']}"
          f"  ip={r['ip']}", flush=True)
    if flags:
        for fl in flags:
            print(f"           !! {fl}", flush=True)

    # emit ProbeResult for verifier
    # compact: magic(H) node(B) tick(I) dns(H) tcp(H) tls(H) ttfb(H) ent_x100(H) cert_fp(8s)
    try:
        pkt = struct.pack("!HBIHHHHH8s",
                          PR_MAGIC, NODE_ID, tick,
                          int(r["dns_ms"]),
                          int(r["tcp_ms"]),
                          int(r["tls_ms"]),
                          int(r["ttfb_ms"]),
                          int(r["entropy"] * 100),
                          r["cert_fp"][:8].encode())
        pr_out.sendto(pkt, (PR_GRP, PR_PORT))
    except OSError:
        pass
