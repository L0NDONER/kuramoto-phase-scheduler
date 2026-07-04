#!/usr/bin/env python3
"""
nazare_wan.py — Nazaré-WAN/SIN.

Domain: timing plausibility of the raw Nazaré sine on the WAN, before any
WireGuard encapsulation. Calibrated to ISP jitter, fibre path variation,
router queueing, and the Nazaré sine's own modulation — nothing about
what happens once traffic enters the tunnel.

Proves: arrival plausibility of the sine packets; timestamp within
Δ_WAN ± ε_WAN of local now; the WAN jitter envelope; pacing of the sine
independent of WG.

Must refuse: anything about WG timing (that's nazare_wg.py's domain),
anything about encrypted flows, anything about identity or semantics.
This is a local verifier of WAN timing, not tunnel timing — see
[[project_verification_lattice]].
"""
from nazare import Nazare, NazareConfig

DELTA_WAN_S = 0.15     # wider than WG — raw ISP/fibre jitter
EPSILON_WAN_S = 0.05


def make_verifier():
    return Nazare(NazareConfig(
        name="nazare-wan",
        delta_s=DELTA_WAN_S,
        epsilon_s=EPSILON_WAN_S,
        verdict_pass="WAN-SIN-TIMING-PLAUSIBLE",
        verdict_fail="WAN-SIN-TIMING-IMPLAUSIBLE",
    ))
