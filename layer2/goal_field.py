"""goal_field.py — long-horizon accumulator of Layer 1's own tracking
quality, binned by which region of phase space psi1 was sitting in when
each sample landed.

Purely observational. Each tick already produces an instantaneous
(psi1, r1, dt_lag_s) reading (run_layer1.py) -- psi_bands.py-style
classification only ever looks at ONE of those readings at a time. This
accumulates a slow exponential moving average of r and |dt_lag_s| per
angular bin of psi1, so a bin that consistently shows high r_H and low
dt_H_ms is a phase-space region this stack has, historically, tracked
well; a bin that never accumulates good numbers is one it hasn't. That's
the "goal field": a map of which regimes have been easy vs hard, built
from nothing but averaging real per-tick readings over time -- no
learning, no gradients, no model of *why* a region is easy, just the
accumulated record that it has been.

Deliberately does not feed back into gain, actuation, or anything else
in this codebase -- this is a report, not a controller. Per
feedback_substrate_not_a_cron_job: the substrate emits a signal,
actuation is always a separate consumer's independent decision. Wiring
this field's output into a reflex arc would be exactly the "substrate
self-directs actuation" inversion that principle exists to prevent --
out of scope here by design, not by oversight.
"""
import math
from dataclasses import dataclass, field

N_BINS = 12                    # 30 degrees per bin over (-pi, pi]
BIN_WIDTH = 2 * math.pi / N_BINS


def bin_index(psi):
    """Which of N_BINS equal-width angular bins psi falls in, wrapping
    (-pi, pi] the same way every other phase value in this codebase
    does."""
    wrapped = ((psi + math.pi) % (2 * math.pi)) - math.pi
    idx = int((wrapped + math.pi) / BIN_WIDTH)
    return min(idx, N_BINS - 1)   # guard the wrapped==pi edge case


@dataclass
class BinState:
    r_h: float = 0.0
    dt_h_ms: float = 0.0
    n_samples: int = 0


class GoalField:
    def __init__(self, alpha):
        """alpha is the EMA weight given to each new sample -- see
        run_layer1.py for how it's derived from a chosen long-horizon
        time constant, not guessed here."""
        self.alpha = alpha
        self.bins = [BinState() for _ in range(N_BINS)]

    def update(self, psi1, r1, dt_lag_s):
        b = self.bins[bin_index(psi1)]
        dt_ms = abs(dt_lag_s) * 1000.0
        if b.n_samples == 0:
            # First sample in this bin: seed the EMA at the sample itself
            # instead of averaging against an arbitrary 0.0 starting
            # point, which would bias a freshly-visited bin's early
            # numbers toward "good" for no real reason.
            b.r_h, b.dt_h_ms = r1, dt_ms
        else:
            b.r_h += self.alpha * (r1 - b.r_h)
            b.dt_h_ms += self.alpha * (dt_ms - b.dt_h_ms)
        b.n_samples += 1

    def report(self, min_samples=5):
        """Bins with at least min_samples, sorted best-to-worst by
        r_h descending then dt_h_ms ascending -- highest sustained
        coherence, lowest sustained tracking-error-in-time, matching the
        module docstring's definition of 'easy' vs 'hard'. Returns a list
        of (bin_center_rad, BinState) tuples; printing/using this is the
        caller's job, this function doesn't act on it."""
        scored = [
            (i * BIN_WIDTH - math.pi + BIN_WIDTH / 2, b)
            for i, b in enumerate(self.bins) if b.n_samples >= min_samples
        ]
        scored.sort(key=lambda x: (-x[1].r_h, x[1].dt_h_ms))
        return scored

    def meta_coherence(self):
        """S = sum_band r_h(band) * w(band), w(band) = that band's share
        of total samples seen (n_samples(band) / sum(n_samples)) -- an
        occupancy weight, not a guessed constant: a band the system has
        actually spent most of its history in counts for more than one
        it briefly visited once. S in [0,1] the same range r itself is
        in, since w sums to 1 by construction -- reads as "coherence,
        weighted by how much of the system's real history was spent in
        each regime", not per-tick r1/r2, which only ever describes the
        instant it was read. Returns 0.0 if nothing has been observed
        yet (no evidence of coherence, not proof of its absence)."""
        total_n = sum(b.n_samples for b in self.bins)
        if total_n == 0:
            return 0.0
        return sum(b.r_h * (b.n_samples / total_n) for b in self.bins)
