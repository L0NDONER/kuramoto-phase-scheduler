"""layer2_oscillator.py — Layer 2's own phase, RK4-integrated forward in
time and forced toward the mean-field psi computed from incoming Layer 1
reports (a moving target). Byte-similar copy of layer1/layer1_oscillator.py
-- same recursive-fractal reasoning layer0_oscillator.py's copies already
document: this exact operator (mean-field target -> meta-state gain ->
RK4 phase) is reused unchanged one level further up, only the source
layer changes (Layer 0 reports -> Layer 1 reports).

Layer 0's static-reference entrainment (dtheta = omega + k*sin(-theta))
is the target=0 special case of the general forced form used here:
dtheta = omega + k*sin(target - theta). Same RK4 shape, same K > omega
stability/lock condition, just generalized to a target that moves every
interval instead of sitting at 0 forever -- exactly the same relationship
Layer 1 has to Layer 0, one level up.

Coupling gain is driven by the same three-signal meta-state as Layer 1's
gain_from_meta_state(): r2 (do the live Layer 1 sources agree?),
tracking_err1 (is Layer 1 itself well-anchored to what IT tracks, i.e.
low tracking error against its own target?), and delta_tracking_err2 (is
Layer 2's own lock currently improving or degrading?). Mechanical, no
learning, same shape recursing one level further.
"""
import math
import random

OMEGA2 = 2 * math.pi / 0.03   # same 30ms carrier period as Layer 0/1's,
                               # own instance -- see layer1_oscillator.py's
                               # note on why these stay equal for now

K2_MAX = 8000.0                # same scale as Layer 0/1's K_MAX/K1_MAX

# Same K*dt<~2.8 (RK4 linear stability) and dt<<1/K reasoning as
# layer0_oscillator.py / layer1_oscillator.py -- sizing dt off K2_MAX
# keeps K*dt~0.5 across the whole gain range Layer 2 can produce.
DT2_S = 1.0 / (2.0 * K2_MAX)


class Layer2Node:
    def __init__(self, rng=None):
        rng = rng or random.Random(0)
        self.theta = rng.uniform(0, 2 * math.pi)
        self.omega = OMEGA2

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


# Meta-state gain -- identical shape to layer1_oscillator.py's
# gain_from_meta_state(), one level up: r2/tracking_err1/delta_tracking_err2
# instead of r1/tracking_err0/delta_tracking_err1. See that module's
# docstring for the full reasoning behind each factor; not repeated here
# beyond the renaming, this is deliberately the same operator.
TRACKING_ERR1_UNLOCK = math.pi / 2       # same boundary used one level down
DIVERGENCE_DAMPING_SCALE = math.pi / 4   # delta_tracking_err2 magnitude that halves stability_factor


def gain_from_meta_state(r2, tracking_err1, delta_tracking_err2):
    """Same three-factor trust product as layer1_oscillator.py's function
    of the same name, one level up -- see that module for the reasoning."""
    coherence_factor = max(0.0, min(1.0, r2))
    anchoring_factor = max(0.0, 1.0 - abs(tracking_err1) / TRACKING_ERR1_UNLOCK)
    stability_factor = max(0.0, min(1.0,
        1.0 - max(0.0, delta_tracking_err2) / DIVERGENCE_DAMPING_SCALE))
    return K2_MAX * coherence_factor * anchoring_factor * stability_factor


def integrate_interval(node, gain, target, interval_s, dt=DT2_S):
    """Runs interval_s worth of RK4 substeps, holding gain and target
    fixed for the whole interval -- same zero-order-hold convention as
    Layer 0/1's integrate_interval."""
    n_substeps = max(1, int(round(interval_s / dt)))
    for _ in range(n_substeps):
        node.step_rk4(dt, gain, target)
