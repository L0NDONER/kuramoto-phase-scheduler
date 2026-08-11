"""market_telemetry.py — realized-volatility fraction from a market price
feed, same shape as mint_cpu_layer0/cpu_telemetry.py's load_frac: a bounded
value_frac fed straight into layer0_oscillator.gain_from_deviation()
unchanged (see that module's docstring -- only the telemetry source
changes across layers, the gain function stays generic).

Realized vol is stddev of log returns over a rolling window, normalized
against vol_ref (the vol level that saturates gain at K_MAX). Target is
0.0 ("calm") by convention -- deviation from calm is what pulls the
oscillator's phase back toward the reference, same role CPU load's
distance from TARGET_LOAD_FRAC plays. Pick a TARGET_CALM_FRAC > 0 instead
if the feed has structural baseline noise that shouldn't drive gain at
rest.

VOL_REF=0.02 is a guessed default sized for a synthetic random walk, not
real trade-tick data -- confirmed too high by ~100x against real Binance
BTCUSDT ticks (2026-08-11), which produced value_frac~0 and never locked.
Same shape as the gpu_layer0 K_BASE mistuning (project_kuramoto_engineering_pitfalls):
a threshold guessed instead of measured against the real geometry. Use
calibrate() against a live warm-up window instead of trusting the default.

First sample() call (and any call before window_size+1 prices have
arrived) has no full return window to compute stddev from -- returns
None, same as CpuTelemetry's first-sample handling.
"""
import math
from collections import deque
from dataclasses import dataclass

WINDOW_SIZE = 30          # ticks per rolling window
VOL_REF = 0.02            # guessed fallback only -- see module docstring, use calibrate()
TARGET_CALM_FRAC = 0.0    # "calm" reference -- see module docstring
CALIB_MULTIPLIER = 4.0    # vol_ref = this many x the observed calm baseline


@dataclass
class MarketSample:
    value_frac: float    # realized vol / VOL_REF, clamped to [0, 1]
    realized_vol: float  # raw stddev of log returns, unclamped


class MarketVolTelemetry:
    def __init__(self, window_size=WINDOW_SIZE, vol_ref=VOL_REF):
        self.window_size = window_size
        self.vol_ref = vol_ref
        self._prices = deque(maxlen=window_size + 1)

    def ingest(self, price):
        """Feed one price tick as the market feed delivers it. Call
        sample() separately, once per report interval."""
        self._prices.append(price)

    def sample(self):
        """Returns a MarketSample, or None if fewer than window_size+1
        prices have been ingested yet (no full return window)."""
        if len(self._prices) < self.window_size + 1:
            return None
        prices = list(self._prices)
        returns = [math.log(prices[i] / prices[i - 1])
                   for i in range(1, len(prices))]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        realized_vol = math.sqrt(variance)
        value_frac = min(1.0, realized_vol / self.vol_ref)
        return MarketSample(value_frac=value_frac, realized_vol=realized_vol)

    def calibrate(self, baseline_vol, multiplier=CALIB_MULTIPLIER):
        """Sets vol_ref from an observed calm-period realized_vol (e.g.
        the mean of several sample() readings taken during warm-up)
        instead of the guessed module default. multiplier sizes how many
        multiples of calm-baseline vol should saturate gain -- 4x calm by
        default, same "how far off target before K_MAX" role K_BASE/K_MAX
        play for the CPU/GPU layers, just derived from data here instead
        of hand-picked.

        A degenerate baseline_vol==0 (warm-up window happened to catch a
        genuinely flat price -- thin trading, not a bug) can't be turned
        into a vol_ref by scaling: multiplying zero is still zero, and an
        epsilon floor there would make vol_ref effectively zero too,
        saturating gain on the very next tick regardless of how calm the
        market actually is. Keep the existing vol_ref (module default on
        first call) instead of calibrating off a zero reading -- caller
        should re-run calibrate() once real movement has been observed."""
        if baseline_vol <= 0:
            return
        self.vol_ref = baseline_vol * multiplier
