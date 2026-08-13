"""layer1_oscillator.py — Layer 1's own phase, RK4-integrated forward in
time and forced toward the mean-field psi computed from incoming Layer 0
reports (a moving target), not toward a fixed reference like Layer 0's
theta=0.

layer0_report.py's docstring already scoped this gap: "Layer 1 just
takes an instantaneous mean-field snapshot of whatever psi values arrive
... it doesn't RK4-integrate its own carrier ... build that properly
later if the simple version isn't enough, don't pretend it's already
there." This is that build.

Layer 0's static-reference entrainment (dtheta = omega + k*sin(-theta),
see mint_cpu_layer0/layer0_oscillator.py) is the target=0 special case of
the general forced form used here: dtheta = omega + k*sin(target -
theta). Same RK4 shape, same K > omega stability/lock condition, just
generalized to a target that moves every interval instead of sitting at
0 forever.

Coupling gain is driven by a meta-state built from three geometric
signals (see gain_from_meta_state() below), not r1 alone -- r1 (do the
Layer 0 sources agree?), tracking_err0 (is Layer 0 itself well-anchored
to its own reference?), and delta_tracking_err1 (is Layer 1's own lock
currently improving or degrading?). Layer 1 doesn't just track psi1
anymore, it also tracks how well it's tracking, and uses that to decide
how hard to keep trying -- purely mechanical (no learning, no gradients),
same "don't wire noise straight into a forced lock" caution as before,
just informed by more than one signal now.
"""
import math
import random

OMEGA1 = 2 * math.pi / 0.03   # same 30ms carrier period as Layer 0's OMEGA0,
                               # own instance -- no reason for these to be the
                               # same value beyond both needing "well above any
                               # dt this module chooses", kept equal for now

K1_MAX = 8000.0                # same scale as Layer0's K_MAX -- see that
                                # module's docstring for the K*dt stability
                                # derivation this mirrors

# Same K*dt<~2.8 (RK4 linear stability) and dt<<1/K reasoning as
# mint_cpu_layer0/layer0_oscillator.py -- sizing dt off K1_MAX keeps
# K*dt~0.5 across the whole gain range Layer 1 can produce.
DT1_S = 1.0 / (2.0 * K1_MAX)


class Layer1Node:
    def __init__(self, rng=None):
        rng = rng or random.Random(0)
        self.theta = rng.uniform(0, 2 * math.pi)
        self.omega = OMEGA1

    def _dtheta(self, theta, k, target):
        # Forcing toward a moving target, strength k -- see module
        # docstring; Layer 0's forced-to-zero is the target=0 case of
        # this same form.
        return self.omega + k * math.sin(target - theta)

    def step_rk4(self, dt, k, target):
        t = self.theta
        k1 = self._dtheta(t, k, target)
        k2 = self._dtheta(t + 0.5 * dt * k1, k, target)
        k3 = self._dtheta(t + 0.5 * dt * k2, k, target)
        k4 = self._dtheta(t + dt * k3, k, target)
        self.theta = (t + (dt / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)) % (2 * math.pi)



# Meta-state gain: folds three geometric signals into how much Layer 1
# trusts its own forcing, not just upstream agreement (r1) alone --
# mechanical, no learning, same "compare trends, don't fit them" spirit
# as the rest of this codebase.
#
#   - r1 (coherence_factor): do the live Layer 0 sources currently agree
#     with each other? Same signal gain_from_coherence used alone before.
#   - tracking_err0 (anchoring_factor): is Layer 0 itself well-anchored
#     to ITS OWN reference (theta=0), independent of whether sources
#     agree with each other? Two sources can agree tightly on a psi that
#     is itself far off Layer 0's own reference -- r1 alone can't see
#     that, this can. Same pi/2 unlock boundary layer0_oscillator.py /
#     psi_bands.py already use elsewhere in this stack.
#   - delta_tracking_err1 (stability_factor): is Layer 1's own tracking
#     error currently growing (diverging) or shrinking (converging)? A
#     growing error while already being forced reads as "the target is
#     currently harder to track than the coupling can follow", not
#     "push harder" -- same "don't fight incoherence by cranking gain"
#     reasoning as order_parameter_pruned() leaving a straying node's own
#     forcing untouched rather than weakening it. Only the growing case
#     is penalized; a shrinking error (converging) isn't rewarded beyond
#     what r1/tracking_err0 already grant.
TRACKING_ERR0_UNLOCK = math.pi / 2       # same boundary as layer0_oscillator.py's PRUNE_ERR_BAND
DIVERGENCE_DAMPING_SCALE = math.pi / 4   # delta_tracking_err1 magnitude that halves stability_factor


def gain_from_meta_state(r1, tracking_err0, delta_tracking_err1):
    """Replaces gain_from_coherence(r1) with a three-factor trust product
    -- see the constants above and module docstring for what each factor
    means and why it's mechanical, not learned. Each factor clamps to
    [0,1] independently, so any one bad signal alone can drive gain to
    ~0 without the others needing to agree first."""
    coherence_factor = max(0.0, min(1.0, r1))
    anchoring_factor = max(0.0, 1.0 - abs(tracking_err0) / TRACKING_ERR0_UNLOCK)
    stability_factor = max(0.0, min(1.0,
        1.0 - max(0.0, delta_tracking_err1) / DIVERGENCE_DAMPING_SCALE))
    return K1_MAX * coherence_factor * anchoring_factor * stability_factor


def integrate_interval(node, gain, target, interval_s, dt=DT1_S):
    """Runs interval_s worth of RK4 substeps, holding gain and target
    fixed for the whole interval -- same zero-order-hold convention as
    Layer 0's integrate_interval (target/gain sampled once per interval,
    not every substep)."""
    n_substeps = max(1, int(round(interval_s / dt)))
    for _ in range(n_substeps):
        node.step_rk4(dt, gain, target)
