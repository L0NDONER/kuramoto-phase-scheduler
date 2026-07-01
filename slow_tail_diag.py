#!/usr/bin/env python3
"""
slow_tail_diag.py — identify source of τ₂≈1000-tick slow autocorrelation tail.

Collects 90s of AxisPulse and decomposes the slow tail into:
  A) absolute phase drift   (theta1 residual, theta2 residual individually)
  B) relative phase (pd)    (what we've been measuring)
  C) timing jitter          (inter-packet interval autocorrelation)

If slow tail is in A → clock/CPU scheduling drift
If slow tail only in B, not A → coupling dynamics or LP filter artifact
If slow tail in C → network/OS jitter creating apparent phase noise
"""
import socket, struct, time, math

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

COLLECT_S = 90.0
TICK_S    = 0.050   # beacon period

def collect(duration_s):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", AP_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))
    s.settimeout(0.5)
    rows = []; t_end = time.monotonic() + duration_s
    print(f"Collecting {duration_s:.0f}s...", flush=True)
    while time.monotonic() < t_end:
        try: data, _ = s.recvfrom(64)
        except socket.timeout: continue
        if len(data) < AP_SIZE: continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] == AP_MAGIC and f[2]:
            rows.append((time.monotonic(), f[3], f[4], f[5], abs(f[6]), f[7]))
            # (wall_t, tick, theta1, theta2, pd, pd_dev)
    print(f"  {len(rows)} locked samples", flush=True)
    return rows

def autocorr(xs, lag):
    n = len(xs)
    if lag >= n: return float('nan')
    a = xs[:n-lag]; b = xs[lag:]
    ma = sum(a)/len(a); mb = sum(b)/len(b)
    num = sum((x-ma)*(y-mb) for x,y in zip(a,b))
    da = math.sqrt(sum((x-ma)**2 for x in a))
    db = math.sqrt(sum((y-mb)**2 for y in b))
    return num/(da*db) if da*db > 0 else 0.0

def detrend(xs):
    """Remove linear trend."""
    n = len(xs)
    t = list(range(n))
    mt = (n-1)/2; mx = sum(xs)/n
    num = sum((t[i]-mt)*(xs[i]-mx) for i in range(n))
    den = sum((t[i]-mt)**2 for i in range(n))
    slope = num/den if den else 0
    return [xs[i] - slope*t[i] for i in range(n)]

def fit_tau(acf_pairs, max_lag=None):
    """Single-exponential log-linear fit."""
    pairs = [(k, r) for k, r in acf_pairs if not math.isnan(r) and r > 0.05]
    if max_lag: pairs = [(k, r) for k, r in pairs if k <= max_lag]
    if len(pairs) < 3: return None
    xs = [k for k, _ in pairs]; ys = [math.log(r) for _, r in pairs]
    n = len(xs); sx=sum(xs); sy=sum(ys); sx2=sum(x**2 for x in xs); sxy=sum(x*y for x,y in zip(xs,ys))
    denom = n*sx2 - sx**2
    if abs(denom) < 1e-12: return None
    slope = (n*sxy - sx*sy)/denom
    return -1/slope if slope < 0 else None

def unwrap_phase(phases):
    """Unwrap a phase sequence to remove 2π jumps."""
    result = [phases[0]]
    for i in range(1, len(phases)):
        diff = phases[i] - result[-1]
        while diff > math.pi:  diff -= 2*math.pi
        while diff < -math.pi: diff += 2*math.pi
        result.append(result[-1] + diff)
    return result

rows = collect(COLLECT_S)

wall_ts  = [r[0] for r in rows]
ticks    = [r[1] for r in rows]
theta1s  = [r[2] for r in rows]
theta2s  = [r[3] for r in rows]
pds      = [r[4] for r in rows]

# ── A: absolute phase residuals ───────────────────────────────────────────────
# Unwrap phases (they're raw, may wrap at 2π), then detrend to remove steady rotation
th1_uw = unwrap_phase(theta1s)
th2_uw = unwrap_phase(theta2s)
th1_res = detrend(th1_uw)
th2_res = detrend(th2_uw)
pd_res  = detrend(pds)

# ── B: timing jitter ─────────────────────────────────────────────────────────
# Inter-arrival intervals (wall clock between successive AxisPulse packets)
intervals = [wall_ts[i+1] - wall_ts[i] for i in range(len(wall_ts)-1)]
iv_mean = sum(intervals)/len(intervals)
iv_res  = [x - iv_mean for x in intervals]

print(f"\n=== Signal statistics ===")
def stats(name, xs):
    m = sum(xs)/len(xs)
    std = math.sqrt(sum((x-m)**2 for x in xs)/len(xs))
    print(f"  {name:20s}  mean={m:+.6f}  std={std:.6f}")

stats("theta1 residual", th1_res)
stats("theta2 residual", th2_res)
stats("pd residual", pd_res)
stats("interval residual (ms)", [x*1000 for x in iv_res])

# ── Autocorrelation at even lags (same-oscillator) ───────────────────────────
LAGS = [1,2,3,4,5,6,8,10,12,15,20,25,30]
TAU_FAST = 10.0; TAU_SLOW = 1000.0

print(f"\n{'lag':>4}  {'r(pd)':>8}  {'r(th1)':>8}  {'r(th2)':>8}  {'r(jitter)':>10}  {'pred_fast':>10}")
for bt in LAGS:
    ap = bt * 2
    r_pd = autocorr(pd_res,  ap)
    r_t1 = autocorr(th1_res, ap)
    r_t2 = autocorr(th2_res, ap)
    r_iv = autocorr(iv_res,  ap) if ap < len(iv_res) else float('nan')
    pred = math.exp(-bt/TAU_FAST)
    print(f"{bt:>4}  {r_pd:>+8.4f}  {r_t1:>+8.4f}  {r_t2:>+8.4f}  {r_iv:>+10.4f}  {pred:>10.4f}")

# ── Fit τ to each signal ──────────────────────────────────────────────────────
print(f"\n=== Fitted τ (ticks) ===")
for name, xs in [("pd", pd_res), ("theta1", th1_res), ("theta2", th2_res)]:
    acf = [(bt, autocorr(xs, bt*2)) for bt in range(1, 31)]
    tau_short = fit_tau(acf, max_lag=8)
    tau_full  = fit_tau(acf)
    ts = f"{tau_short:.1f}" if tau_short else "N/A"
    tf = f"{tau_full:.1f}" if tau_full else "N/A"
    print(f"  {name:8s}  τ_short(1-8)={ts}  τ_full={tf}")

acf_iv = [(bt, autocorr(iv_res, bt*2)) for bt in range(1, 31)]
tau_iv = fit_tau(acf_iv)
ti = f"{tau_iv:.1f}" if tau_iv else "N/A"
print(f"  {'jitter':8s}  τ_full={ti}")

# ── Cross-correlation: does theta1 drift predict pd drift? ───────────────────
print(f"\n=== Cross-correlations at lag 0 ===")
def crosscorr(xs, ys):
    n = min(len(xs), len(ys))
    xs = xs[:n]; ys = ys[:n]
    mx = sum(xs)/n; my = sum(ys)/n
    num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    dx = math.sqrt(sum((x-mx)**2 for x in xs))
    dy = math.sqrt(sum((y-my)**2 for y in ys))
    return num/(dx*dy) if dx*dy > 0 else 0.0

print(f"  corr(theta1, pd) = {crosscorr(th1_res, pd_res):+.4f}")
print(f"  corr(theta2, pd) = {crosscorr(th2_res, pd_res):+.4f}")
print(f"  corr(theta1, theta2) = {crosscorr(th1_res, th2_res):+.4f}")
print(f"  corr(jitter, pd) = {crosscorr(iv_res, pd_res[1:]):+.4f}")

# ── Conclusion ────────────────────────────────────────────────────────────────
print(f"\n=== Diagnosis ===")
r_t1_slow = autocorr(th1_res, 30*2)
r_t2_slow = autocorr(th2_res, 30*2)
r_pd_slow = autocorr(pd_res, 30*2)
r_iv_slow = autocorr(iv_res, 30*2) if 60 < len(iv_res) else 0

print(f"  r_theta1(lag=30)  = {r_t1_slow:+.4f}")
print(f"  r_theta2(lag=30)  = {r_t2_slow:+.4f}")
print(f"  r_pd(lag=30)      = {r_pd_slow:+.4f}")
print(f"  r_jitter(lag=30)  = {r_iv_slow:+.4f}")

if abs(r_t1_slow) > 0.3 or abs(r_t2_slow) > 0.3:
    print("  → SLOW TAIL IN ABSOLUTE PHASES: clock drift / CPU thermal / scheduler")
    if abs(r_t1_slow - r_t2_slow) < 0.1:
        print("    Both beacons drift together → common-mode clock source")
    else:
        print("    Beacons drift independently → per-process scheduling jitter")
elif abs(r_iv_slow) > 0.3:
    print("  → SLOW TAIL IN TIMING JITTER: OS scheduler / network pacing")
else:
    print("  → SLOW TAIL IN pd ONLY: coupling dynamics or LP filter artifact")
    print("    (not in absolute phases or timing — emergent from relative motion)")
