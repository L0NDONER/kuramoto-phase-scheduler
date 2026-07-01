#!/usr/bin/env python3
"""
perturb_test.py — Kuramoto falsification test.

Protocol:
  Phase 1 (20s)  — baseline pd recording from AxisPulse
  Phase 2        — inject N fake sid=1 beacon packets with theta+delta on 239.0.0.1:7400
  Phase 3 (60s)  — record pd recovery after injection
  Analysis       — fit exponential decay, compare τ_measured vs τ_theory

Kuramoto prediction (anti-phase lock, linearised):
  d(ε)/dt = -2K·ε  →  ε(t) = ε₀·exp(-t/τ)  where τ = 1/(2K)
  With LP_ALPHA=0.20 filtering peer_lp:  τ_eff ≈ 1/(2K·LP_ALPHA)
  K ≈ 0.25, LP_ALPHA = 0.20  →  τ_eff ≈ 10 ticks = 0.5s
"""
import socket, struct, time, math, sys, subprocess

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

# Raw beacon multicast (239.0.0.1:7400) — inject here
BCN_GRP  = "239.0.0.1"; BCN_PORT = 7400
BCN_FMT  = ">HBIffBQ";  BCN_MAGIC = 0x1B4A   # 24 bytes, big-endian packed

K_NOM      = 0.25
LP_ALPHA   = 0.20
TAU_THEORY = 1.0 / (2 * K_NOM)                  # ticks (pure Kuramoto)
TAU_EFF    = 1.0 / (2 * K_NOM * LP_ALPHA)        # ticks (with LP filter)
TICK_S     = 0.050                               # 50ms per beacon tick

BASELINE_S   = 20.0
INJECT_DELTA = 0.10   # rad — small enough to stay locked (ANTI_THRESH=0.22)
INJECT_N     = 5      # burst of fakes between real ticks
RECOVER_S    = 30.0
PI2_HOST     = "pi2"

def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.settimeout(0.5)
    return s

def read_beacon1(ap_sock):
    """Read a few AxisPulse packets and return last (theta1, tick)."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            data, _ = ap_sock.recvfrom(64)
        except socket.timeout:
            continue
        if len(data) < AP_SIZE:
            continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] == AP_MAGIC and f[2]:
            return f[4], f[3]   # theta1 (phases[1] = sid=1), tick
    raise RuntimeError("No locked AxisPulse within 3s")

def get_beacon1_pid():
    result = subprocess.run(["ssh", PI2_HOST, "pgrep -a beacon"],
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "pi":
            return int(parts[0])
    raise RuntimeError(f"beacon sid=1 not found: {result.stdout!r}")

def inject_sustained(theta1, last_tick, delta, stop_s, hz, tx_sock):
    """Inject fake sid=1 packets at hz rate for stop_s seconds."""
    dst   = (BCN_GRP, BCN_PORT)
    shifted = (theta1 + delta) % (2 * math.pi)
    count = 0
    t_end = time.monotonic() + stop_s
    tick  = last_tick
    interval = 1.0 / hz
    while time.monotonic() < t_end:
        pkt = struct.pack(BCN_FMT, BCN_MAGIC, 1, tick, shifted, 0.054, 0, 0)
        tx_sock.sendto(pkt, dst)
        tick  += 1
        count += 1
        time.sleep(interval)
    print(f"  injected {count} packets  theta1={theta1:.3f}  delta={delta:+.3f}  → {shifted:.3f}", flush=True)
    return tick

def record_pd(sock, duration_s, label, locked_only=True):
    """Record (time, pd, locked) triples for duration_s seconds."""
    samples = []
    t_end = time.monotonic() + duration_s
    while time.monotonic() < t_end:
        try:
            data, _ = sock.recvfrom(64)
        except socket.timeout:
            continue
        if len(data) < AP_SIZE:
            continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] != AP_MAGIC:
            continue
        locked = bool(f[2])
        if locked_only and not locked:
            continue
        pd = abs(f[6])
        samples.append((time.monotonic(), pd, locked))
    locked_n = sum(1 for _, _, l in samples if l)
    print(f"  [{label}] {len(samples)} samples ({locked_n} locked)", flush=True)
    return samples

def fit_exponential(samples, t_perturb, baseline_mean):
    """
    Fit |pd(t) - baseline| = A·exp(-(t-t_perturb)/τ) to post-perturbation samples.
    Uses absolute deviation — works whether pd is perturbed above or below baseline.
    """
    post = [(t - t_perturb, abs(pd - baseline_mean))
            for t, pd, _ in samples
            if t >= t_perturb and abs(pd - baseline_mean) > 0.01]
    if len(post) < 5:
        return None, None
    xs = [t for t, _ in post]
    ys = [math.log(max(y, 1e-9)) for _, y in post]
    n  = len(xs)
    sx = sum(xs); sy = sum(ys); sx2 = sum(x**2 for x in xs); sxy = sum(x*y for x,y in zip(xs,ys))
    denom = n*sx2 - sx*sx
    if abs(denom) < 1e-12:
        return None, None
    slope = (n*sxy - sx*sy) / denom
    intercept = (sy - slope*sx) / n
    A   = math.exp(intercept)
    tau = -1.0 / slope if slope < 0 else None
    return A, tau

# ── Main ─────────────────────────────────────────────────────────────────────
sock = _mcast_in(AP_GRP, AP_PORT)

print("=== Kuramoto perturbation test ===", flush=True)
print(f"  τ_theory (pure)  = {TAU_THEORY:.1f} ticks = {TAU_THEORY*TICK_S*1000:.0f} ms", flush=True)
print(f"  τ_eff (LP-corrected) = {TAU_EFF:.1f} ticks = {TAU_EFF*TICK_S*1000:.0f} ms", flush=True)
print(flush=True)

# Phase 1: baseline
print(f"Phase 1: {BASELINE_S:.0f}s baseline...", flush=True)
baseline_samples = record_pd(sock, BASELINE_S, "baseline", locked_only=True)
if not baseline_samples:
    print("No AxisPulse received — is the substrate running?"); sys.exit(1)
pd_values = [pd for _, pd, _ in baseline_samples]
baseline_mean = sum(pd_values) / len(pd_values)
baseline_std  = math.sqrt(sum((p - baseline_mean)**2 for p in pd_values) / len(pd_values))
print(f"  pd baseline  mean={baseline_mean:.4f}  std={baseline_std:.4f}", flush=True)

# Phase 2: small burst injection — stays within linear regime (ANTI_THRESH=0.22)
print(f"\nPhase 2: small injection (delta={INJECT_DELTA:+.3f} rad, n={INJECT_N})...", flush=True)
tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
theta1, last_tick = read_beacon1(sock)
# send burst quickly (< one beacon tick) so real beacon doesn't intervene between packets
dst = (BCN_GRP, BCN_PORT)
shifted = (theta1 + INJECT_DELTA) % (2 * math.pi)
for i in range(INJECT_N):
    pkt = struct.pack(BCN_FMT, BCN_MAGIC, 1, last_tick + i, shifted, 0.054, 0, 0)
    tx_sock.sendto(pkt, dst)
print(f"  injected {INJECT_N} packets  theta1={theta1:.3f}  → {shifted:.3f}", flush=True)
t_resume = time.monotonic()

# Phase 3: recovery
print(f"\nPhase 3: {RECOVER_S:.0f}s recovery recording...", flush=True)
recover_samples = record_pd(sock, RECOVER_S, "recovery", locked_only=False)

# Analysis
print("\n=== Analysis ===", flush=True)
post = [(t, pd) for t, pd, _ in recover_samples if t >= t_resume]
if post:
    peak_dev = max(abs(pd - baseline_mean) for _, pd in post[:80])  # peak in first 2s post-resume
    print(f"  peak |pd - baseline| after resume: {peak_dev:.4f} rad  (injected {INJECT_DELTA:.2f} rad)", flush=True)

A, tau_s = fit_exponential(recover_samples, t_resume, baseline_mean)
if tau_s is not None:
    tau_ticks = tau_s / TICK_S
    print(f"\n  τ_measured = {tau_s*1000:.0f} ms  ({tau_ticks:.1f} ticks)", flush=True)
    print(f"  τ_theory   = {TAU_THEORY*TICK_S*1000:.0f} ms  ({TAU_THEORY:.1f} ticks)  [pure Kuramoto]", flush=True)
    print(f"  τ_eff      = {TAU_EFF*TICK_S*1000:.0f} ms  ({TAU_EFF:.1f} ticks)  [LP-corrected]", flush=True)

    ratio_pure = tau_s / (TAU_THEORY * TICK_S)
    ratio_eff  = tau_s / (TAU_EFF * TICK_S)

    print(f"\n  ratio to τ_theory: {ratio_pure:.2f}x", flush=True)
    print(f"  ratio to τ_eff:    {ratio_eff:.2f}x", flush=True)

    # verdict
    if 0.5 < ratio_eff < 2.0:
        verdict = "CONFIRMED — recovery consistent with LP-corrected Kuramoto prediction"
    elif 0.5 < ratio_pure < 2.0:
        verdict = "PLAUSIBLE — matches pure Kuramoto but LP correction doesn't explain it"
    else:
        verdict = f"FALSIFIED — measured τ is {ratio_eff:.1f}x the LP-corrected prediction"
    print(f"\n  {verdict}", flush=True)
else:
    print("  Recovery fit failed — perturbation may have been too small or recovery too fast", flush=True)

# Save raw data
out_path = "/tmp/perturb_result.csv"
with open(out_path, "w") as f:
    f.write("phase,t_abs,pd\n")
    for t, pd, lk in baseline_samples:
        f.write(f"baseline,{t:.4f},{pd:.6f},{int(lk)}\n")
    for t, pd, lk in recover_samples:
        f.write(f"recovery,{t:.4f},{pd:.6f},{int(lk)}\n")
print(f"\n  raw data → {out_path}", flush=True)
