"""layer0_oscillator.py — per-node Kuramoto phase, coupled to real telemetry.

Byte-identical copy of mint_cpu_layer0/layer0_oscillator.py -- deliberately
not adapted or renamed. The whole point of the fractal-hierarchy idea is
that this exact operator (telemetry deviation -> gain -> RK4 phase ->
order parameter) is reused unchanged across layers; only the telemetry
source and the deviation's physical meaning change (GPU power fraction,
CPU load fraction, here realized-volatility fraction from market_telemetry.py).
If this file ever needs to diverge between copies, that's worth noticing
before doing it, not doing it by default -- keep them in sync unless
there's a real reason not to.

Reference phase is fixed at theta=0 ("on target"). Each node's telemetry
deviation from a target fraction modulates its coupling GAIN, not its
natural frequency omega -- a constant omega offset would be a permanent
one-directional drift (pitfall #1 from project_grid_twin /
project_kuramoto_engineering_pitfalls), not a proportional response to
how far off target the node currently is. omega itself stays a shared
carrier with small, zero-mean heterogeneity across nodes.

RK4 substeps at dt well below the carrier period (pitfall #2): with
OMEGA0 chosen for a ~30ms natural period, dt=2ms is ~15 substeps per
period, comfortably below the "order of magnitude under the fastest
natural frequency" bar.
"""
import cmath
import math
import random

OMEGA0 = 2 * math.pi / 0.03   # ~209.4 rad/s -- 30ms carrier period
OMEGA_HETEROGENEITY = 0.02    # +/- fraction of OMEGA0, zero-mean, per-node

# Static-reference entrainment (forcing toward a fixed phase, not toward
# another moving oscillator) only has a stable lock point when k > OMEGA0
# -- same shape as the 2-oscillator Kuramoto sync condition. Confirmed
# live 2026-08-10 (gpu_layer0, real Kaggle T4s): K_BASE=40 meant only a
# ~100% deviation (never realistically happens) ever crossed OMEGA0, so
# every plausible telemetry deviation (10-50%) produced non-converging,
# oscillating r instead of a lock. Retuned so even a 10% deviation
# clears OMEGA0 with real margin (~1.4x), not just barely.
K_BASE = 6000.0               # coupling gain per unit deviation from target
K_MAX = 8000.0                 # clamp -- deviation can't drive gain unboundedly

# dt must also stay well below the fastest timescale K introduces (1/K),
# not just below the carrier period -- the same pitfall #2 lesson, but
# triggered by a fast timescale this module created itself once K got
# large enough to lock. Confirmed live on real Kaggle T4s 2026-08-10:
# at the previous DT_S=0.002, K*dt~6.6 for realistic deviations -- way
# outside RK4's linear stability region (needs K*dt below ~2.8) -- so r
# oscillated wildly between ~0 and ~1 every interval instead of locking.
# Sizing dt off K_MAX directly keeps K*dt ~0.5 across the entire gain
# range, not just the low end.
DT_S = 1.0 / (2.0 * K_MAX)    # ~6.25e-5s at K_MAX=8000 -- K_MAX*DT_S=0.5


class Layer0Node:
    """One node's phase state (a GPU, a CPU core, whatever this layer's
    telemetry source is)."""

    def __init__(self, index, rng=None):
        rng = rng or random.Random(index)
        self.index = index
        self.theta = rng.uniform(0, 2 * math.pi)   # random initial phase
        self.omega = OMEGA0 * (1.0 + rng.uniform(-OMEGA_HETEROGENEITY,
                                                   OMEGA_HETEROGENEITY))

    def _dtheta(self, theta, k):
        # Forcing toward the reference phase (0), strength k -- not a
        # bias on omega, see module docstring.
        return self.omega + k * math.sin(-theta)

    def step_rk4(self, dt, k):
        t = self.theta
        k1 = self._dtheta(t, k)
        k2 = self._dtheta(t + 0.5 * dt * k1, k)
        k3 = self._dtheta(t + 0.5 * dt * k2, k)
        k4 = self._dtheta(t + dt * k3, k)
        self.theta = (t + (dt / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)) % (2 * math.pi)


def gain_from_deviation(value_frac, target_frac):
    """Telemetry -> coupling gain (not omega). Bigger deviation from the
    target fraction => stronger pull toward the reference phase."""
    deviation = abs(value_frac - target_frac)
    return min(K_MAX, K_BASE * deviation)


def integrate_interval(nodes, gains, interval_s, dt=DT_S):
    """Runs interval_s worth of RK4 substeps for every node, each held at
    its own gains[i] for the whole interval (telemetry is sampled once per
    interval, not every substep -- see README's zero-order-hold note)."""
    n_substeps = max(1, int(round(interval_s / dt)))
    for _ in range(n_substeps):
        for node, k in zip(nodes, gains):
            node.step_rk4(dt, k)


def order_parameter(nodes):
    """r*e^(i*psi) = mean(e^(i*theta)). r in [0,1]: 1 = fully coherent
    (all nodes currently near the reference phase), 0 = scattered.
    NOT a meaningful swarm statistic below ~10 oscillators -- confirmed
    concretely in gpu_layer0/sweep_convergence.py (N=2 can hit r=1.0
    from free-running beat frequency alone, not just from a real lock).
    Report it anyway (it's real math on real data), just don't read
    consensus into it at low N."""
    if not nodes:
        return 0.0, 0.0
    z = sum(cmath.exp(1j * n.theta) for n in nodes) / len(nodes)
    return abs(z), cmath.phase(z)


# A node's own tracking error against the fixed reference (theta=0) that
# still counts as "coherent enough to report" -- widened from the pi/4
# "holding" boundary to pi/2, psi_bands.py's "marginal" boundary (the
# actual unlock point: sin(err)=omega/k stops having a solution past
# this). So "marginal" nodes -- lock real but thin margin -- still get
# counted; only "unanchored" nodes (past the point where a stable lock
# could even exist) get excluded. A node beyond this is pruned from
# order_parameter_pruned()'s average, not from its own forcing -- see
# that function's docstring for why those are different things.
PRUNE_ERR_BAND = math.pi / 2


def node_tracking_err(node):
    """This node's own phase error against the fixed reference theta=0,
    wrapped to (-pi, pi]. Same formula layer1_oscillator.py uses against
    a moving target, specialized to Layer 0's target=0."""
    return ((node.theta + math.pi) % (2 * math.pi)) - math.pi


def coherent_nodes(nodes):
    """Nodes whose own tracking_err is still inside PRUNE_ERR_BAND.
    Pruning here never touches a node's own gain/forcing -- an excluded
    node keeps being pulled back toward the reference exactly as before
    (see gain_from_deviation/step_rk4), it's just left out of what gets
    reported upward until it's back in-band. Reducing a straying node's
    own gain instead would weaken its correction and likely make it drift
    further, not recover faster -- that's not what this does."""
    return [n for n in nodes if abs(node_tracking_err(n)) < PRUNE_ERR_BAND]


def order_parameter_pruned(nodes):
    """Same order_parameter() math, computed over coherent_nodes(nodes)
    instead of the full ensemble -- a handful of nodes currently far off
    the reference (marginal/unanchored, not just noisy) would otherwise
    corrupt the reported r/psi into looking more scattered than the
    coherent majority actually is. Falls back to the full ensemble if
    pruning would leave nothing to report, same empty-input behavior as
    order_parameter(). Returns (r, psi, coherent) so callers can see which
    nodes were actually counted."""
    coherent = coherent_nodes(nodes)
    r, psi = order_parameter(coherent if coherent else nodes)
    return r, psi, coherent
