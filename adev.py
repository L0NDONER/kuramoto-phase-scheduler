#!/usr/bin/env python3
"""
adev.py — Allan deviation characterisation of the Kuramoto substrate.

Allan deviation is the standard metric for oscillator frequency stability.
For the presence chain, ADEV sets the gap-detection threshold: a gap longer
than the τ where ADEV turns upward produces a detectable phase discontinuity.

Usage:
    python3 adev.py --live <seconds>          # collect fresh data
    python3 adev.py --csv <perturb_result>    # use existing baseline CSV
    python3 adev.py --chain <checkpoint_log>  # characterise presence chain

Output: ADEV(τ) table + noise-type identification + gap threshold.
"""
import socket, struct, time, math, sys, json, csv as _csv

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

TICK_S  = 0.010   # quartz_beacon.py tick period — one tick = one crystal step

# ── Allan deviation ────────────────────────────────────────────────────────────
def adev(phase_s, tau0_s, taus=None):
    """
    Overlapping Allan deviation from a phase time series (in seconds or radians).
    phase_s : sequence of phase values, equally spaced at tau0_s
    tau0_s  : spacing between samples (seconds)
    taus    : list of averaging times to evaluate (default: powers of 2)
    Returns list of (tau, adev, n_pairs) triples.
    """
    x = list(phase_s)
    N = len(x)
    if taus is None:
        m = 1
        taus_m = []
        while m <= N // 3:
            taus_m.append(m)
            m *= 2
    else:
        taus_m = [max(1, round(t / tau0_s)) for t in taus]

    results = []
    for m in taus_m:
        tau = m * tau0_s
        # overlapping estimator: sum (x[i+2m] - 2*x[i+m] + x[i])^2
        n = N - 2 * m
        if n < 2:
            continue
        s = sum((x[i + 2*m] - 2*x[i + m] + x[i])**2 for i in range(n))
        avar = s / (2.0 * tau**2 * n)
        results.append((tau, math.sqrt(avar), n))
    return results

def noise_type(adev_results):
    """
    Classify noise type from slope of log(ADEV) vs log(τ).
    Slope ≈ -1  : white phase noise (WPN)
    Slope ≈ -0.5: white frequency noise (WFN) / flicker phase
    Slope ≈  0  : flicker frequency noise (FFN) — the floor
    Slope ≈ +0.5: random walk frequency noise (RWFN) — drift
    Slope ≈ +1  : frequency drift
    """
    if len(adev_results) < 3:
        return []
    types = []
    for i in range(1, len(adev_results)):
        tau_a, ad_a, _ = adev_results[i-1]
        tau_b, ad_b, _ = adev_results[i]
        slope = math.log(ad_b / ad_a) / math.log(tau_b / tau_a)
        if slope < -0.75:   label = "WPN  (white phase)"
        elif slope < -0.25: label = "WFN  (white freq / flicker phase)"
        elif slope < 0.25:  label = "FFN  (flicker freq — floor)"
        elif slope < 0.75:  label = "RWFN (random walk freq)"
        else:               label = "DRIFT"
        types.append((tau_b, slope, label))
    return types

def gap_threshold(adev_results):
    """
    Find τ where ADEV stops falling (flicker floor) — beyond this, drift is detectable.
    This is the maximum undetectable gap in the presence chain.
    """
    if len(adev_results) < 3:
        return None
    # find minimum ADEV
    min_ad = min(ad for _, ad, _ in adev_results)
    min_tau = next(tau for tau, ad, _ in adev_results if ad == min_ad)
    return min_tau, min_ad

# ── data sources ──────────────────────────────────────────────────────────────
def collect_live(duration_s, sid=1):
    """
    Only accept packets from one sid. AxisPulse theta1 is fresh only on
    sid=1 packets (theta2 is last-known-stale there), and vice versa for
    sid=2 — mixing sids corrupts both the tick sequence (two independent
    per-node counters colliding in one integer space) and the phase series
    (fresh samples interleaved with stale copies). Filtering to one sid
    keeps both clean.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", AP_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))
    s.settimeout(0.5)
    rows = []; seen = set(); t_end = time.monotonic() + duration_s
    print(f"Collecting {duration_s:.0f}s from sid={sid}...", flush=True)
    while time.monotonic() < t_end:
        try: data, _ = s.recvfrom(64)
        except socket.timeout: continue
        if len(data) < AP_SIZE: continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] != AP_MAGIC or not f[2] or f[1] != sid: continue
        tick = f[3]
        if tick in seen: continue
        seen.add(tick)
        rows.append({"tick": tick, "theta1": f[4], "theta2": f[5], "pd": abs(f[6])})
    rows.sort(key=lambda r: r["tick"])
    print(f"  {len(rows)} unique locked ticks (sid={sid})", flush=True)
    return rows

def load_csv(path):
    rows = []
    for r in _csv.DictReader(open(path)):
        if r["phase"] == "baseline":
            rows.append({"pd": float(r["pd"])})
    return rows

def load_chain(path):
    rows = []
    for line in open(path):
        line = line.strip()
        if line.startswith("{"):
            r = json.loads(line)
            rows.append(r)
    rows.sort(key=lambda r: r["tick"])
    return rows

# ── main ──────────────────────────────────────────────────────────────────────
def unwrap(phases):
    out = [phases[0]]
    for i in range(1, len(phases)):
        d = phases[i] - out[-1]
        while d >  math.pi: d -= 2*math.pi
        while d < -math.pi: d += 2*math.pi
        out.append(out[-1] + d)
    return out

if __name__ == "__main__":
    rows = None; tau0 = TICK_S; label = ""

    if "--live" in sys.argv:
        idx = sys.argv.index("--live")
        dur = float(sys.argv[idx+1]) if idx+1 < len(sys.argv) else 120.0
        sid = 1
        if "--sid" in sys.argv:
            sid = int(sys.argv[sys.argv.index("--sid")+1])
        rows = collect_live(dur, sid=sid)
        pd_series   = [r["pd"] for r in rows]
        # own theta is fresh every packet at this sid; peer's theta in this
        # packet is a stale copy — only the own series is valid for ADEV.
        own_key = "theta1" if sid == 1 else "theta2"
        th1_series  = unwrap([r[own_key] for r in rows])
        tau0 = TICK_S; label = f"live AxisPulse (sid={sid})"

    elif "--csv" in sys.argv:
        idx = sys.argv.index("--csv")
        rows = load_csv(sys.argv[idx+1])
        pd_series  = [r["pd"] for r in rows]
        tau0 = TICK_S * 2   # baseline CSV sampled at AP rate (2× beacon ticks)
        label = sys.argv[idx+1]

    elif "--chain" in sys.argv:
        idx = sys.argv.index("--chain")
        rows = load_chain(sys.argv[idx+1])
        pd_series   = [r["pd"] for r in rows]
        th1_series  = [r.get("theta1", 0) for r in rows]
        tick_diffs  = [rows[i+1]["tick"] - rows[i]["tick"] for i in range(len(rows)-1)]
        tau0 = (sum(tick_diffs)/len(tick_diffs)) * TICK_S if tick_diffs else TICK_S * 100
        label = sys.argv[idx+1]

    else:
        # default: live 120s
        rows = collect_live(120.0)
        pd_series  = [r["pd"] for r in rows]
        th1_series = unwrap([r["theta1"] for r in rows])
        th2_series = unwrap([r["theta2"] for r in rows])
        tau0 = TICK_S; label = "live 120s"

    print(f"\n=== Allan Deviation — {label} ===")
    print(f"  samples={len(pd_series)}  tau0={tau0*1000:.1f}ms")

    # ── phase difference (pd) ADEV ────────────────────────────────────────────
    print(f"\n--- pd (phase difference, anti-phase deviation) ---")
    print(f"{'tau_s':>10}  {'tau_ticks':>10}  {'ADEV':>12}  {'n_pairs':>8}  noise")
    ad_pd = adev(pd_series, tau0)
    nt    = noise_type(ad_pd)
    nt_map = {t: (s, l) for t, s, l in nt}
    for tau_s, ad, n in ad_pd:
        ticks = tau_s / TICK_S
        nm = nt_map.get(tau_s, (0, ""))[1]
        print(f"{tau_s:>10.3f}  {ticks:>10.1f}  {ad:>12.6f}  {n:>8}  {nm}")

    gap_t = gap_threshold(ad_pd)
    if gap_t:
        g_tau, g_ad = gap_t
        print(f"\n  Flicker floor: ADEV={g_ad:.6f} at τ={g_tau:.3f}s ({g_tau/TICK_S:.0f} ticks)")
        print(f"  Gap threshold: gaps > {g_tau:.1f}s ({g_tau/TICK_S:.0f} ticks) produce detectable phase drift")

    # ── absolute phase ADEV (theta1) if available ─────────────────────────────
    if "th1_series" in dir() and th1_series:
        own_label = own_key if "own_key" in dir() else "theta1"
        print(f"\n--- {own_label} (absolute phase, detrended) ---")
        # detrend: remove linear fit
        n = len(th1_series)
        t_arr = list(range(n))
        mt = (n-1)/2; mx = sum(th1_series)/n
        num = sum((t_arr[i]-mt)*(th1_series[i]-mx) for i in range(n))
        den = sum((t_arr[i]-mt)**2 for i in range(n))
        slope = num/den
        th1_dt = [th1_series[i] - slope*t_arr[i] for i in range(n)]
        print(f"  linear drift = {slope/TICK_S*1e6:.2f} μrad/s")

        print(f"{'tau_s':>10}  {'tau_ticks':>10}  {'ADEV':>12}  {'n_pairs':>8}  noise")
        ad_th1 = adev(th1_dt, tau0)
        nt1    = noise_type(ad_th1)
        nt1_map = {t: (s, l) for t, s, l in nt1}
        for tau_s, ad, n in ad_th1:
            ticks = tau_s / TICK_S
            nm = nt1_map.get(tau_s, (0, ""))[1]
            print(f"{tau_s:>10.3f}  {ticks:>10.1f}  {ad:>12.6f}  {n:>8}  {nm}")
