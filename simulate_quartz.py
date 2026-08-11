#!/usr/bin/env python3
"""
simulate_quartz.py — synthetic Kuramoto+PI oscillator, current vs quartz-disciplined.

There's no oscillator *model* inside kuramoto_analysis.py to swap — that script only
fits an ACF to real pd samples pulled live from beacon hardware. This builds the
missing piece: a discrete-time simulator of the same Kuramoto+PI recurrence, driven
by per-tick phase noise, so we can compare a noisy free-running oscillator against
a quartz-disciplined one (much lower per-tick phase noise) on the *same* analysis
pipeline (autocorr / two-timescale fit / bootstrap) from kuramoto_analysis.py.

  e[n+1] = e[n]*(1 - 2*K_NOM*LP_ALPHA) - I[n] + noise[n]
  I[n+1] = I[n]*INTEG_LEAK + KI*e[n]

Run:
    python3 simulate_quartz.py
"""
import random

from kuramoto_analysis import (
    K_NOM, LP_ALPHA, KI, INTEG_LEAK, TICK_S, TAU_FAST, TAU_SLOW,
    even_lag_acf, fit_two_timescale, bootstrap_tau,
)

N_TICKS = 20000        # beacon ticks per oscillator per run
SIGMA_CURRENT = 0.02   # rad/tick phase noise — free-running (RC/software clock)
SIGMA_QUARTZ  = 0.0005 # rad/tick phase noise — quartz-disciplined (~40x tighter)

DECAY = 1.0 - 2 * K_NOM * LP_ALPHA


def simulate_series(sigma, n=N_TICKS, seed=None):
    rng = random.Random(seed)
    e, integ = rng.gauss(0, sigma), 0.0
    out = []
    for _ in range(n):
        out.append(e)
        integ = integ * INTEG_LEAK + KI * e
        e = e * DECAY - integ + rng.gauss(0, sigma)
    return out


def make_pds(sigma, n=N_TICKS, seed_base=0):
    """Interleave two independent sid series like real AxisPulse (sid=1,2 alternating)."""
    e1 = simulate_series(sigma, n, seed=seed_base)
    e2 = simulate_series(sigma, n, seed=seed_base + 1)
    pds = [0.0] * (2 * n)
    pds[0::2] = e1
    pds[1::2] = e2
    return pds


def analyze(label, sigma):
    pds = make_pds(sigma)
    mean = sum(pds) / len(pds)
    c = [p - mean for p in pds]
    acf = even_lag_acf(c, max_bt=20)
    tau1, tau2, A, B, loss = fit_two_timescale(acf)
    mean_t, std_t, p5, p95 = bootstrap_tau(pds, n_boot=200, block_size=100)
    print(f"\n=== {label} (sigma={sigma:.4f} rad/tick) ===")
    print(f"  tau1 = {tau1:.1f} ticks = {tau1*TICK_S*1000:.0f}ms  (A={A:.2f})")
    print(f"  tau2 = {tau2:.1f} ticks = {tau2*TICK_S:.1f}s      (B={B:.2f})")
    if mean_t:
        print(f"  bootstrap tau1: {mean_t:.1f} +/- {std_t:.1f}  90% CI [{p5:.1f}, {p95:.1f}]")
    return tau1


if __name__ == "__main__":
    print(f"Theoretical tau_fast (Kuramoto+LP) = {TAU_FAST:.1f} ticks")
    print(f"Theoretical tau_slow (PI leak)      = {TAU_SLOW:.0f} ticks")

    tau1_current = analyze("current (free-running)", SIGMA_CURRENT)
    tau1_quartz  = analyze("quartz-disciplined", SIGMA_QUARTZ)

    ratio = tau1_quartz / tau1_current
    print(f"\n=== Verdict ===")
    print(f"  tau1 current: {tau1_current:.1f} ticks   tau1 quartz: {tau1_quartz:.1f} ticks")
    print(f"  ratio: {ratio:.1f}x")
    if 50 <= tau1_quartz - tau1_current <= 200 or ratio >= 3:
        print("  CONFIRMED: quartz substrate shows a dramatic tau1 jump vs free-running —")
        print("  consistent with higher coherence / slower relaxation from lower phase noise.")
    else:
        print("  NOT CONFIRMED at these noise levels — tau1 jump is not dramatic.")
