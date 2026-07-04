#!/usr/bin/env python3
"""
nazare_wg.py — Nazaré-WG.

Domain: timing plausibility of a message *after* WireGuard encapsulation.
Calibrated to tunnel scheduling, kernel crypto batching, peer RTT, and EC2
host jitter — nothing upstream of the tunnel.

Proves: arrival plausibility post-tunnel; timestamp within Δ_WG ± ε_WG of
local now; pacing/jitter inside the encrypted channel.

Must refuse: anything about the raw WAN, anything about the Nazaré sine
on the WAN side (that's nazare_wan.py's domain), anything about remote
truth or identity. This is a local verifier of tunnel timing, not global
timing — see [[project_verification_lattice]].
"""
from nazare import Nazare, NazareConfig

DELTA_WG_S = 0.05     # tune against observed WG RTT
EPSILON_WG_S = 0.02   # tunnel/crypto-batching slack


def make_verifier():
    return Nazare(NazareConfig(
        name="nazare-wg",
        delta_s=DELTA_WG_S,
        epsilon_s=EPSILON_WG_S,
        verdict_pass="WG-TIMING-PLAUSIBLE",
        verdict_fail="WG-TIMING-IMPLAUSIBLE",
    ))
