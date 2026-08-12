"""run_test.py — wires a real price feed (crypto via Binance, or a US
stock via Finnhub -- see price_feed.py) into the Layer0 oscillator loop,
same shape as mint_cpu_layer0/run_local.py.

Feed source is chosen by MARKET_FEED_SOURCE ('crypto' or 'stock', env
var), symbol by MARKET_SYMBOL or the second CLI arg -- see price_feed.py
for why this is a selector over two interchangeable backends rather than
a hardcoded Binance import. Everything below this point (bars, telemetry,
oscillator, pruning, psi bands) is completely feed-agnostic: it only ever
sees a stream of (price) floats, never knows or cares whether they came
from a crypto trade or an equity trade.

Raw trade prints are aggregated into BAR_INTERVAL_S bars (see
bar_feed.py) before they ever reach MarketVolTelemetry -- ingest() is
called once per bar close, not once per trade print. sample() is called
once per REPORT_INTERVAL_S off a separate timer, same zero-order-hold
convention layer0_oscillator.py already documents for the CPU/GPU layers
(telemetry sampled once per interval, not every substep). Two different
intervals, two different jobs: bars decide what counts as one return
observation, REPORT_INTERVAL_S decides how often the oscillator reads the
current vol estimate.

One market-wide vol reading drives gain uniformly across all N_NODES --
unlike per-core CPU load, there's a single feed here, not N independent
telemetry sources.
"""
import asyncio
import os
import sys

from bar_feed import bar_stream
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter_pruned)
from layer0_report import mcast_out, send_report
from market_telemetry import MarketVolTelemetry, TARGET_CALM_FRAC
from price_feed import price_stream
from psi_bands import psi_band

NODE_NAME = "market"
FEED_SOURCE = os.environ.get("MARKET_FEED_SOURCE", "crypto")
_DEFAULT_SYMBOLS = {"crypto": "solusdt", "stock": "AAPL"}
SYMBOL = (sys.argv[2] if len(sys.argv) > 2 else
          os.environ.get("MARKET_SYMBOL", _DEFAULT_SYMBOLS.get(FEED_SOURCE, "solusdt")))
N_NODES = 8
REPORT_INTERVAL_S = 0.5
BAR_INTERVAL_S = 1.0     # trade prints aggregated into one bar per this many seconds
# No arg -> run forever (production default, meant for a persistent
# service). Pass an explicit duration for a bounded test run instead.
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else None
CALIB_SAMPLES = 10       # sample() readings averaged into the calm baseline
CALIB_INTERVAL_S = BAR_INTERVAL_S   # one poll per bar -- polling faster than
                                     # bars arrive would just re-read the same
                                     # sample() value and skew the average


async def calibrate_vol_ref(telem, max_attempts=6):
    """Warm-up phase: wait for the rolling window to fill, then average
    CALIB_SAMPLES realized_vol readings from live (presumed-calm) market
    data and set vol_ref from that instead of the guessed module default
    -- see market_telemetry.py's module docstring for why the default is
    wrong for real tick data.

    calibrate() refuses a zero baseline (see its docstring), so a warm-up
    window that happened to catch a flat price retries against a fresh
    window rather than silently keeping the guessed default forever."""
    while telem.sample() is None:
        await asyncio.sleep(CALIB_INTERVAL_S)

    for attempt in range(1, max_attempts + 1):
        readings = []
        while len(readings) < CALIB_SAMPLES:
            await asyncio.sleep(CALIB_INTERVAL_S)
            sample = telem.sample()
            if sample is not None:
                readings.append(sample.realized_vol)

        baseline = sum(readings) / len(readings)
        if baseline > 0:
            telem.calibrate(baseline)
            print(f"[calibrate] baseline realized_vol={baseline:.6f} over "
                  f"{len(readings)} samples (attempt {attempt}) -> "
                  f"vol_ref={telem.vol_ref:.6f}", flush=True)
            return

        print(f"[calibrate] attempt {attempt}: flat window (baseline=0), "
              f"retrying...", flush=True)

    print(f"[calibrate] gave up after {max_attempts} flat attempts, "
          f"keeping default vol_ref={telem.vol_ref:.6f}", flush=True)


async def main():
    telem = MarketVolTelemetry()
    last_price = None

    async def consume():
        nonlocal last_price
        async for price in bar_stream(price_stream(FEED_SOURCE, SYMBOL), BAR_INTERVAL_S):
            telem.ingest(price)
            last_price = price

    consumer = asyncio.create_task(consume())

    await calibrate_vol_ref(telem)

    nodes = [Layer0Node(i) for i in range(N_NODES)]
    report_sock = mcast_out()

    tick = 0
    while TOTAL_DURATION_S is None or tick * REPORT_INTERVAL_S < TOTAL_DURATION_S:
        await asyncio.sleep(REPORT_INTERVAL_S)

        sample = telem.sample()
        value_frac = sample.value_frac if sample else 0.0
        gain = gain_from_deviation(value_frac, TARGET_CALM_FRAC)
        gains = [gain] * N_NODES

        integrate_interval(nodes, gains, REPORT_INTERVAL_S)
        r, psi, coherent = order_parameter_pruned(nodes)
        send_report(report_sock, NODE_NAME, r, psi)

        pruned = [n.index for n in nodes if n not in coherent]

        vol_str = f"{sample.realized_vol:.5f}" if sample else "warming up"
        price_str = f"{last_price:.2f}" if last_price is not None else "n/a"
        band = psi_band(psi, r)
        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f} "
              f"[{band:>10}]  price={price_str:>10}  realized_vol={vol_str}  "
              f"value_frac={value_frac:.3f}  gain={gain:7.1f}  "
              f"pruned={pruned}", flush=True)
        tick += 1

    consumer.cancel()


if __name__ == "__main__":
    asyncio.run(main())
