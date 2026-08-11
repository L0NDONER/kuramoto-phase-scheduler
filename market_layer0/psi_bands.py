"""psi_bands.py — semantic labels for the oscillator's mean-field phase.

psi only means something when the ensemble is actually locked (r close to
1) -- order_parameter() in layer0_oscillator.py already notes r itself
isn't a trustworthy statistic at low coherence/low N, and an unlocked
psi is just wherever the scattered mean-field vector happens to point,
not a signal.

When locked, though, psi isn't arbitrary. Static-reference entrainment
(forcing every node toward theta=0, see layer0_oscillator.py's module
docstring) has its stable lock point where omega + k*sin(-psi) = 0, i.e.
sin(psi) = omega/k -- the same k > OMEGA0 condition that docstring already
establishes for a lock to exist at all. So |psi| is a direct readout of
how much margin the current gain k has over that boundary: small |psi|
means k comfortably exceeds omega (the market vol driving this layer is
calm relative to what the coupling can absorb), |psi| approaching pi/2
means k is barely above omega -- the lock is real but about to break if
gain drops any further.

Bands are named for the market layer's calm-anchor framing (theta=0 =
"calm", see market_telemetry.py's TARGET_CALM_FRAC), not generic
Kuramoto terminology -- this module is specific to layers using static-
reference entrainment toward theta=0, not a general phase classifier.
"""
import math

R_LOCK_THRESHOLD = 0.8   # below this, r is too low to trust psi at all

# (upper bound on |psi| in radians, label), checked in ascending order
_BANDS = [
    (math.pi / 12, "anchored"),   # <15deg -- k >> omega, comfortable margin
    (math.pi / 4,  "holding"),    # <45deg -- locked, real but not a tight margin
    (math.pi / 2,  "marginal"),   # <90deg -- close to the unlock boundary
]


def psi_band(psi, r):
    """Returns a short semantic label for the current (psi, r) reading:
    'scattered' when r hasn't cleared R_LOCK_THRESHOLD (psi meaningless),
    'anchored'/'holding'/'marginal' by |psi| when locked, or 'unanchored'
    past the pi/2 unlock boundary (locked by r's threshold but psi says
    the lock shouldn't exist -- worth flagging, not silently dropping)."""
    if r < R_LOCK_THRESHOLD:
        return "scattered"
    abs_psi = abs(psi)
    for bound, label in _BANDS:
        if abs_psi < bound:
            return label
    return "unanchored"
