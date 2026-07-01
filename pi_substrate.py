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
import socket, struct, math, time, collections

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
REPORT   = 500
ANALYSIS = 10        # deep analysis every ANALYSIS reports
WIN      = 500       # keep enough for histogram
CLIP_MS  = 25.0      # outlier threshold

acc = 0.0
_last_t = None
_deltas = collections.deque(maxlen=WIN)


def _quantile(s, q):
    i = q * (len(s) - 1)
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (i - lo) * (s[hi] - s[lo])


def _analyse(deltas):
    s = sorted(deltas)
    ms = [d * 1000 for d in s]
    mu = sum(ms) / len(ms)
    sd = math.sqrt(sum((d - mu) ** 2 for d in ms) / len(ms))

    clipped = [d for d in ms if d <= CLIP_MS]
    if clipped:
        mu_c = sum(clipped) / len(clipped)
        sd_c = math.sqrt(sum((d - mu_c) ** 2 for d in clipped) / len(clipped))
    else:
        mu_c = sd_c = float("nan")

    p50 = _quantile(ms, 0.50)
    p90 = _quantile(ms, 0.90)
    p95 = _quantile(ms, 0.95)
    p99 = _quantile(ms, 0.99)

    buckets = [0, 5, 10, 15, 20, 25, 30, 40, 50, float("inf")]
    hist = [0] * (len(buckets) - 1)
    for d in ms:
        for i in range(len(buckets) - 1):
            if buckets[i] <= d < buckets[i + 1]:
                hist[i] += 1
                break

    n = len(ms)
    print(f"  --- Δt analysis  n={n}  clip={CLIP_MS}ms ---", flush=True)
    print(f"  full:    μ={mu:.2f}ms  σ={sd:.3f}ms", flush=True)
    print(f"  clipped: μ={mu_c:.2f}ms  σ={sd_c:.3f}ms  ({len(clipped)}/{n} kept)", flush=True)
    print(f"  p50={p50:.2f}  p90={p90:.2f}  p95={p95:.2f}  p99={p99:.2f}  max={ms[-1]:.2f}ms", flush=True)
    bar = ""
    for i, count in enumerate(hist):
        lo = buckets[i]; hi = buckets[i + 1]
        label = f"{lo:.0f}-{hi:.0f}" if hi != float("inf") else f"{lo:.0f}+"
        pct = count / n * 100
        bar += f"  {label:>7}ms {count:4d} ({pct:5.1f}%)\n"
    print(bar, end="", flush=True)

print(f"π from substrate  Leibniz series, substrate-paced", flush=True)
print(f"π/4 = 1 − 1/3 + 1/5 − 1/7 + …  one term per locked tick", flush=True)
print(f"Reporting every {REPORT} ticks — Ctrl+C to stop\n", flush=True)

while True:
    data, _ = s.recvfrom(64)
    if len(data) < AP_SIZE:
        continue
    f = struct.unpack_from(AP_FMT, data)
    if f[0] != AP_MAGIC or not f[2]:
        continue

    now = time.perf_counter()
    if _last_t is not None:
        _deltas.append(now - _last_t)
    _last_t = now

    sign = 1 if total % 2 == 0 else -1
    acc  += sign / (2 * total + 1)
    total += 1
    pi_est = 4.0 * acc

    if total % REPORT == 0:
        err = pi_est - math.pi
        if len(_deltas) >= 2:
            ds  = list(_deltas)
            mu  = sum(ds) / len(ds)
            sd  = math.sqrt(sum((d - mu) ** 2 for d in ds) / len(ds))
            mx  = max(ds)
            health = f"  Δt μ={mu*1000:.2f}ms  σ={sd*1000:.3f}ms  max={mx*1000:.2f}ms"
        else:
            health = ""
        print(f"  n={total:8d}  π ≈ {pi_est:.6f}  err={err:+.6f}{health}", flush=True)
        if total % (REPORT * ANALYSIS) == 0 and len(_deltas) >= 10:
            _analyse(list(_deltas))
