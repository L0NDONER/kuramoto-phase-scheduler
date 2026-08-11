"""binance_feed.py — real price ticks from Binance's public trade stream.
No API key needed (public market data). One coroutine, one symbol; yields
raw trade prices as floats, nothing else -- market_telemetry.py owns all
the vol-fraction math, this module's only job is "get real prices out of
the wire".
"""
import asyncio
import json

import websockets

WS_URL_FMT = "wss://stream.binance.com:9443/ws/{symbol}@trade"


async def price_stream(symbol="btcusdt"):
    """Async generator yielding one float price per trade print. Symbol
    is lowercase Binance pair, e.g. 'btcusdt', 'ethusdt'. Reconnects on
    drop -- a public feed stalling shouldn't kill the oscillator loop."""
    url = WS_URL_FMT.format(symbol=symbol)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    yield float(data["p"])
        except (websockets.ConnectionClosed, OSError) as e:
            print(f"[binance_feed] connection dropped ({e}), reconnecting...",
                  flush=True)
            await asyncio.sleep(2)
