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

Coupling gain is driven by r1 (the incoming reports' coherence), not a
telemetry deviation like Layer 0's gain_from_deviation -- when the Layer
0 nodes agree (r1 high), Layer 1 has a real signal worth locking onto;
when they're scattered (r1 low), gain drops toward 0 and Layer 1
free-runs at its own omega instead of forcibly chasing a meaningless
mean-field snapshot. Coherence gates trust in the target -- it isn't
assumed, matching the "don't wire noise straight into a forced lock"
caution from project_kuramoto_engineering_pitfalls.
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


def gain_from_coherence(r1):
    """Layer 0 coherence -> Layer 1's coupling gain. Higher agreement
    among Layer 0 nodes means a more trustworthy target, so gain scales
    directly with r1 (clamped to [0,1]) instead of being constant -- see
    module docstring."""
    return K1_MAX * max(0.0, min(1.0, r1))


def integrate_interval(node, gain, target, interval_s, dt=DT1_S):
    """Runs interval_s worth of RK4 substeps, holding gain and target
    fixed for the whole interval -- same zero-order-hold convention as
    Layer 0's integrate_interval (target/gain sampled once per interval,
    not every substep)."""
    n_substeps = max(1, int(round(interval_s / dt)))
    for _ in range(n_substeps):
        node.step_rk4(dt, gain, target)
