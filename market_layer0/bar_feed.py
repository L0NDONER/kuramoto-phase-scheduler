"""bar_feed.py — aggregates a raw per-trade price stream into fixed-time
bars.

Individual Binance trade prints step in exchange tick-size increments and
fire at whatever rate the market happens to be trading -- at that
granularity, trade-to-trade log returns are dominated by quantization
noise, not real price discovery, which is what made realized_vol swing
from ~0 to saturating gain within a couple of ticks (2026-08-11 run).
Bars (the last trade price observed in a fixed time window) are the
standard unit real market data tooling computes returns over instead of
raw prints, for the same reason.
"""
import asyncio


async def bar_stream(tick_stream, bar_interval_s):
    """Wraps any async generator of raw prices (e.g. binance_feed's
    price_stream) and yields one close price per bar_interval_s -- the
    last trade price observed during that window. Bars with no trade at
    all are skipped (yields nothing that interval) rather than repeating
    the previous close, so a stalled feed shows up as MarketVolTelemetry
    warming up / holding steady, not as fabricated zero-movement bars."""
    last_price = None

    async def consume():
        nonlocal last_price
        async for price in tick_stream:
            last_price = price

    consumer = asyncio.create_task(consume())
    try:
        while True:
            await asyncio.sleep(bar_interval_s)
            if last_price is not None:
                yield last_price
                last_price = None
    finally:
        consumer.cancel()
