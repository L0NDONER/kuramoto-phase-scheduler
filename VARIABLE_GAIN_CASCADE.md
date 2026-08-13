# Variable-Gain Cascade

> I built a physical model and empirically confirmed that a known
> control-systems tradeoff (static high gain trades responsiveness for
> tight lock) holds in this specific implementation — via a controlled
> ablation, not an assumption.

A cascade of RK4-integrated Kuramoto oscillators, each level forced toward a
moving target derived from the level below, with coupling gain that varies
over time rather than staying fixed. Driven by real external telemetry
(crypto price, CPU load) — not synthetic input, not peer-machine timing
exchange (that's `quartz-substrate/`, a separate, unrelated system).

The name is deliberately unglamorous. Earlier candidates (`Trust-Gated
Cascade`, `Coherence-Gated Cascade`) implied that the specific *informed*
judgment behind the gain (coherence, anchoring, stability) is what makes it
work. A controlled ablation showed that's not established — see below. The
one property actually proven necessary is that gain *varies*; not what
decides the variation.

---

## What's proven

**The lock is real and holds.** `market_layer0 → layer1 → layer2` against a
live Binance SOL feed: `r → 1.000`, `S → 0.999` (occupancy-weighted
long-horizon coherence), sustained for 15+ hours continuous, no drift.

**The residual carries real information, in its normal operating regime.**
On live market data, Layer 1's `tracking_err1` correlated `-0.87` with
`gain` (itself driven by real volatility) over 88k+ samples — not measuring
its own ghost, genuinely responsive to small real changes.

**It is specifically blind to fast events.** A real, precisely-timed CPU
load spike (`load_probe/`): raw telemetry detected it immediately
(`z≈7-9`), the residual showed no reliable response (`z≈0-1.4`). Confirmed
via a second, independent test (`deluge_watch/`) against real Deluge
traffic: a sustained single-core deviation locked *more* tightly (smaller
residual) the more anomalous it got, because `gain ∝ |deviation|` rewards
large deviation with large gain, and large gain means tighter lock —
the opposite of what a naive "small residual = calm" reading would assume.

**Depth alone (no gain variation) produces total insensitivity, not
graceful smoothing.** `depth_gate_ablation/`: holding gain fixed at a
realistic constant collapses `tracking_err` to an exact geometric constant
(`sin(err) ≈ omega/gain`, both fixed) that never moves regardless of the
real input — `z = 0.000` exactly, at every depth ≥ 2. Not "weaker
detection" — zero.

**It is not fractal, not connected to Riemann zeta, not "entropy-rejecting"
in any free sense.** Each tested independently, each a clean negative
result. See git history for the actual tests (`marsh.py` fix + rerun,
`depth_gate_ablation`'s `tracking_err0→1→2` scaling check).

## What's NOT proven — the open question

**Whether informed gating (coherence/anchoring/stability) outperforms mere
gain variability.** A third ablation arm — gain drawn randomly each tick,
matched mean *and* std to the real trust-gated version, but with zero
dependence on actual coherence — tracked the real version closely at depth
2 and *exceeded* it at depth 3. If informed gating specifically mattered,
random should have scored near the fixed-gain baseline (`≈0`), not near or
above the real one. It didn't. This is currently unresolved, not
disproven — only one kind of event (a CPU spike) and one informed signal
have been tested against random.

## Directory map

| Path | What it is |
|---|---|
| `market_layer0/` | Layer 0 driven by a live crypto/stock feed (`price_feed.py` selects Binance or Finnhub) |
| `layer1/`, `layer2/` | Recursive cascade, same operator one level up each time |
| `load_probe/` | First controlled test of residual-vs-raw sensitivity, real CPU spike |
| `depth_gate_ablation/` | Isolates cascade depth vs gain-gating as the source of the lock's behavior |
| `regime_classifier/` | Real consumer: CALM/TURBULENT from Layer 1's `r1`, hysteresis, no actuation |
| `deluge_watch/` | Real consumer: sustained per-core deviation detector, with cause breakdown (network vs process) |
| `gpu_layer0/reflex_power_cap.py` | The one real actuation consumer in this codebase — separate, permission-checked, gated by real hardware constraints |

## Architectural boundary, unchanged throughout

The substrate emits a signal. A consumer reads it and decides. The
substrate never decides for itself. This was tested directly — twice,
proposals to wire the substrate's own signal into self-directed action
("trigger exploration," "maximize coherence") were declined for exactly
this reason, consistent with every consumer actually built.
