#!/usr/bin/env python3
"""
kuramoto_analysis.py — two-timescale characterisation of the Kuramoto+PI system.

1. Fit r(k) = A·exp(-k/τ₁) + B·exp(-k/τ₂) to the even-lag autocorrelation
2. Bootstrap CI on τ₁ using block resampling
3. Predict what the autocorrelation would look like with KI=0 (PI integral frozen)

Run on existing baseline data:
    python3 kuramoto_analysis.py /tmp/perturb_result.csv
Or collect fresh baseline:
    python3 kuramoto_analysis.py --live 60
"""
import sys, csv, math, random, socket, struct, time

# ── beacon / AP constants ─────────────────────────────────────────────────────
K_NOM      = 0.25
LP_ALPHA   = 0.20
KP         = 0.10
KI         = 0.00005
INTEG_LEAK = 0.999          # integral *= INTEG_LEAK per tick when unlocked
TICK_S     = 0.050          # beacon tick = 50ms
AP_S       = 0.025          # AxisPulse tick ≈ 25ms (alternating sid=1/sid=2)

# Theoretical time constants
TAU_FAST   = 1.0 / (2 * K_NOM * LP_ALPHA)         # 10 beacon ticks = 500ms (Kuramoto+LP)
TAU_SLOW   = -1.0 / math.log(INTEG_LEAK)           # 1000 beacon ticks = 50s  (PI integral leak)

# ── data loading ──────────────────────────────────────────────────────────────
def load_baseline(path):
    rows = list(csv.DictReader(open(path)))
    return [float(r['pd']) for r in rows if r['phase'] == 'baseline']

def collect_live(duration_s):
    AP_GRP = "239.0.0.2"; AP_PORT = 7404
    AP_FMT = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", AP_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))
    s.settimeout(0.5)
    pds = []; t_end = time.monotonic() + duration_s
    print(f"Collecting {duration_s:.0f}s of baseline...", flush=True)
    while time.monotonic() < t_end:
        try: data, _ = s.recvfrom(64)
        except socket.timeout: continue
        if len(data) < AP_SIZE: continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] == AP_MAGIC and f[2]:
            pds.append(abs(f[6]))
    print(f"  {len(pds)} locked samples", flush=True)
    return pds

# ── autocorrelation (even AP lags only = same-oscillator) ─────────────────────
def autocorr(xs, lag):
    n = len(xs)
    if lag >= n: return float('nan')
    a = xs[:n-lag]; b = xs[lag:]
    ma = sum(a)/len(a); mb = sum(b)/len(b)
    num = sum((x-ma)*(y-mb) for x,y in zip(a,b))
    da = math.sqrt(sum((x-ma)**2 for x in a))
    db = math.sqrt(sum((y-mb)**2 for y in b))
    return num/(da*db) if da*db > 0 else 0.0

def even_lag_acf(pds, max_bt=25):
    """Return list of (beacon_tick_lag, r) for even AP lags."""
    return [(bt, autocorr(pds, bt*2)) for bt in range(1, max_bt+1)]

# ── two-timescale model fit ───────────────────────────────────────────────────
def fit_two_timescale(acf_pairs, tau1_init=TAU_FAST, tau2_init=TAU_SLOW, iters=5000, lr=1e-4):
    """
    Fit r(k) = A·exp(-k/τ₁) + B·exp(-k/τ₂)  with A+B<=1, τ₁<τ₂.
    Simple gradient descent.
    """
    xs = [k for k, r in acf_pairs if not math.isnan(r)]
    ys = [r for k, r in acf_pairs if not math.isnan(r)]

    lnt1 = math.log(tau1_init)
    lnt2 = math.log(tau2_init)
    A, B = 0.7, 0.3

    def predict(lnt1, lnt2, A, B):
        t1, t2 = math.exp(lnt1), math.exp(lnt2)
        return [A*math.exp(-k/t1) + B*math.exp(-k/t2) for k in xs]

    def loss(lnt1, lnt2, A, B):
        preds = predict(lnt1, lnt2, A, B)
        return sum((p-y)**2 for p,y in zip(preds, ys))

    eps = 1e-5
    for i in range(iters):
        L0 = loss(lnt1, lnt2, A, B)
        g_lnt1 = (loss(lnt1+eps, lnt2, A, B) - L0) / eps
        g_lnt2 = (loss(lnt1, lnt2+eps, A, B) - L0) / eps
        g_A    = (loss(lnt1, lnt2, A+eps, B) - L0) / eps
        g_B    = (loss(lnt1, lnt2, A, B+eps) - L0) / eps
        lnt1 -= lr * g_lnt1
        lnt2 -= lr * g_lnt2
        A    -= lr * g_A
        B    -= lr * g_B
        # constrain
        A = max(0.01, min(0.99, A))
        B = max(0.01, min(0.99 - A, B))
        lnt1 = max(math.log(1), min(math.log(50), lnt1))
        lnt2 = max(lnt1 + math.log(2), min(math.log(5000), lnt2))

    return math.exp(lnt1), math.exp(lnt2), A, B, loss(lnt1, lnt2, A, B)

# ── bootstrap CI ─────────────────────────────────────────────────────────────
def bootstrap_tau(pds, n_boot=200, block_size=100):
    """
    Block bootstrap: resample blocks of `block_size` AP samples, compute τ₁
    from log-linear fit on early even lags (beacon ticks 1-6).
    Returns (mean_tau, std_tau, percentile_5, percentile_95).
    """
    n = len(pds)
    n_blocks = n // block_size
    blocks = [pds[i*block_size:(i+1)*block_size] for i in range(n_blocks)]
    taus = []
    for _ in range(n_boot):
        sample_blocks = [random.choice(blocks) for _ in range(n_blocks)]
        sample = [x for block in sample_blocks for x in block]
        c = [p - sum(sample)/len(sample) for p in sample]
        rs = [(bt, autocorr(c, bt*2)) for bt in range(1, 7)]
        rs = [(k, r) for k, r in rs if r > 0.01]
        if len(rs) < 3: continue
        xs = [k for k,_ in rs]; ys = [math.log(r) for _,r in rs]
        n2 = len(xs); sx=sum(xs); sy=sum(ys)
        sx2=sum(x**2 for x in xs); sxy=sum(x*y for x,y in zip(xs,ys))
        denom = n2*sx2 - sx**2
        if abs(denom) < 1e-12: continue
        slope = (n2*sxy - sx*sy)/denom
        if slope >= 0: continue
        taus.append(-1.0/slope)
    if not taus: return None, None, None, None
    taus.sort()
    mean_t = sum(taus)/len(taus)
    std_t  = math.sqrt(sum((t-mean_t)**2 for t in taus)/(len(taus)-1))
    p5  = taus[int(0.05*len(taus))]
    p95 = taus[int(0.95*len(taus))]
    return mean_t, std_t, p5, p95

# ── KI=0 prediction ───────────────────────────────────────────────────────────
def ki0_prediction(max_bt=25):
    """
    With KI=0, the PI integral is frozen at 0. The only relaxation is the
    Kuramoto + LP coupling: r(k) = exp(-k/τ_fast).
    If the long-lag tail disappears in the data, the PI term is confirmed responsible.
    """
    tau_ki0 = 1.0 / (2 * K_NOM * LP_ALPHA)  # same fast timescale, no slow component
    return [(bt, math.exp(-bt/tau_ki0)) for bt in range(1, max_bt+1)]

# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--live" in sys.argv:
        idx = sys.argv.index("--live")
        dur = float(sys.argv[idx+1]) if idx+1 < len(sys.argv) else 60.0
        pds = collect_live(dur)
    elif len(sys.argv) > 1:
        pds = load_baseline(sys.argv[1])
    else:
        print("usage: kuramoto_analysis.py <csv_path>  OR  --live <seconds>")
        sys.exit(1)

    mean = sum(pds)/len(pds)
    std  = math.sqrt(sum((p-mean)**2 for p in pds)/len(pds))
    print(f"\nBaseline: n={len(pds)}  mean={mean:.4f}  std={std:.4f}")
    print(f"Theoretical τ_fast = {TAU_FAST:.1f} ticks = {TAU_FAST*TICK_S*1000:.0f}ms  (Kuramoto+LP)")
    print(f"Theoretical τ_slow = {TAU_SLOW:.0f} ticks = {TAU_SLOW*TICK_S:.0f}s      (PI integral leak)")

    # ── 1. measured ACF ───────────────────────────────────────────────────────
    c = [p - mean for p in pds]
    acf = even_lag_acf(c, max_bt=20)
    print(f"\n{'BT':>3}  {'r_meas':>8}  {'1-τ pred':>9}  {'2-τ pred':>9}  {'KI=0 pred':>10}")

    # ── 2. two-timescale fit ──────────────────────────────────────────────────
    print("\nFitting two-timescale model...", flush=True)
    tau1, tau2, A, B, loss = fit_two_timescale(acf)
    print(f"  τ₁ = {tau1:.1f} ticks = {tau1*TICK_S*1000:.0f}ms  (weight A={A:.2f})")
    print(f"  τ₂ = {tau2:.1f} ticks = {tau2*TICK_S:.1f}s      (weight B={B:.2f})")
    print(f"  residual loss = {loss:.6f}")
    print(f"  τ₁ vs τ_fast:   {tau1/TAU_FAST:.2f}x   (1.0 = exact Kuramoto+LP)")
    print(f"  τ₂ vs τ_slow:   {tau2/TAU_SLOW:.2f}x   (1.0 = exact PI integral leak)")

    ki0 = dict(ki0_prediction(max_bt=20))
    for bt, r in acf:
        pred_1t = math.exp(-bt/TAU_FAST)
        pred_2t = A*math.exp(-bt/tau1) + B*math.exp(-bt/tau2)
        print(f"{bt:>3}  {r:>+8.4f}  {pred_1t:>9.4f}  {pred_2t:>9.4f}  {ki0[bt]:>10.4f}")

    # ── 3. bootstrap CI ───────────────────────────────────────────────────────
    print("\nBootstrap CI on τ₁ (n=200, block=100)...", flush=True)
    mean_t, std_t, p5, p95 = bootstrap_tau(pds, n_boot=200, block_size=100)
    if mean_t:
        print(f"  τ₁ bootstrap: {mean_t:.1f} ± {std_t:.1f} ticks")
        print(f"  90% CI: [{p5:.1f}, {p95:.1f}] ticks = [{p5*TICK_S*1000:.0f}, {p95*TICK_S*1000:.0f}] ms")
        print(f"  τ_fast (predicted) = {TAU_FAST:.1f} ticks — {'INSIDE' if p5 <= TAU_FAST <= p95 else 'OUTSIDE'} 90% CI")

    # ── verdict ───────────────────────────────────────────────────────────────
    print("\n=== Verdict ===")
    tau1_ok  = 0.5 < tau1/TAU_FAST < 2.0
    tau2_ok  = 0.3 < tau2/TAU_SLOW < 3.0
    if tau1_ok and tau2_ok:
        print("CONFIRMED (two-timescale): fast relaxation consistent with Kuramoto+LP,")
        print(f"  slow tail consistent with PI integral (τ₂≈{tau2/TAU_SLOW:.1f}×τ_slow_predicted).")
        print("  Next step: KI=0 test to confirm PI term is responsible for slow component.")
    elif tau1_ok:
        print("PLAUSIBLE (fast component): Kuramoto+LP time constant confirmed.")
        print(f"  Slow tail τ₂={tau2:.0f} ticks does not match PI integral prediction ({TAU_SLOW:.0f} ticks).")
    else:
        print(f"FALSIFIED: τ₁={tau1:.1f} outside 0.5-2× range of τ_fast={TAU_FAST:.1f}.")

    print(f"\nKI=0 TEST: to confirm PI integral is responsible for the slow tail,")
    print(f"  rebuild beacon.c with KI=0 and rerun: python3 kuramoto_analysis.py --live 60")
    print(f"  The slow component should disappear; τ₁ should remain near {TAU_FAST:.0f} ticks.")
