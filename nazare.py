#!/usr/bin/env python3
"""
nazare.py — Nazaré, a pure timing-plausibility verifier.

Proves exactly two things about an already-authenticated, schema-valid
message:

  1. cadence plausibility — the tick it claims to ride was actually
     observed locally (see [[project_verification_lattice]]: this is the
     quartz-timebase node, one layer above the monotonic counter, sitting
     beside it as an incomparable claim — order vs duration).
  2. timestamp plausibility — the latency between that local observation
     and this message's arrival lies within Δ ± ε.

Nothing else. It is not a truth source (it never inspects payload
content), not a remote sensor (it only watches local multicast), and not
a semantic authority (it has no notion of intent, identity, or decision).

This file used to be a "staged-intent, deferred-commit semantic wave
engine": FIFO-driven intent staging, cortex/cerebellum pulse relay, a DCN
feedback loop, phase-geometry decision firing (A.leading, cycle=even,
X/Y binary decisions), pi2 thermal relay, X25519 DH + HMAC signing. All
of that was semantic/oracle behavior riding under the Nazaré name — real
working infrastructure, but outside Nazaré's actual competence (timing
only), and it has been removed rather than relocated: superseded by the
reflex/consensus work done since. See [[project_nazare_timebase_placement]].

nazare_wg.py and nazare_wan.py fork this into the two calibration domains
that must never be merged (WG-tunnel timing vs raw WAN sine timing) — see
carrier_verify.py, which calls whichever fork matches a packet's origin
and never lets one verdict claim the other's domain.
"""
from dataclasses import dataclass


@dataclass
class NazareConfig:
    name: str
    delta_s: float     # timing-plausibility window half-width
    epsilon_s: float   # additional slack for jitter/drift in this domain
    verdict_pass: str
    verdict_fail: str


class Nazare:
    """
    Feed it locally-observed ticks as they happen (`observe`), then check
    a message's claimed tick against that local observation (`check`).
    Never trusts a claim on its own say-so; the local observation log is
    the only ground truth it has, and it is never derived from the
    message being checked.
    """

    def __init__(self, config: NazareConfig):
        self.config = config
        self._seen_at = {}   # tick -> local monotonic arrival time

    def observe(self, tick, arrival_monotonic):
        self._seen_at[tick] = arrival_monotonic

    def check(self, claimed_tick, t_arrival):
        """
        Returns (ok, verdict, detail). `t_arrival` is when *this* verifier
        received the message asserting `claimed_tick` — never a time the
        message itself carries.
        """
        seen_at = self._seen_at.get(claimed_tick)
        if seen_at is None:
            return False, self.config.verdict_fail, (
                f"tick {claimed_tick} never observed locally — cadence unverifiable")

        latency = t_arrival - seen_at
        window = self.config.delta_s + self.config.epsilon_s
        if -self.config.epsilon_s <= latency <= window:
            return True, self.config.verdict_pass, f"latency={latency*1000:.1f}ms within Δ±ε"
        return False, self.config.verdict_fail, (
            f"latency={latency*1000:.1f}ms outside Δ±ε ({window*1000:.1f}ms)")
