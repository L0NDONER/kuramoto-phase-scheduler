"""finnhub_feed.py — real price ticks from Finnhub's free-tier trade
websocket (US stocks). Needs a free API key from finnhub.io, passed via
the FINNHUB_API_KEY environment variable -- see price_feed.py for the
fail-fast check that runs before this module is ever reached, so a
missing key surfaces as a clear message, not a raw connection error.
Same role binance_feed.py plays for crypto, same async-generator-of-
floats contract (see price_feed.py), different wire format underneath.
"""
import asyncio
import json

import websockets

WS_URL_FMT = "wss://ws.finnhub.io?token={api_key}"


async def price_stream(symbol, api_key):
    """Async generator yielding one float price per trade print for
    `symbol` (e.g. 'AAPL'). Reconnects on drop, same shape as
    binance_feed.price_stream -- a stalled feed shouldn't kill the
    oscillator loop. Re-subscribes after every reconnect since Finnhub's
    subscription is per-connection, not persistent server-side state."""
    url = WS_URL_FMT.format(api_key=api_key)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") != "trade":
                        continue
                    for trade in data.get("data", []):
                        yield float(trade["p"])
        except (websockets.ConnectionClosed, OSError) as e:
            print(f"[finnhub_feed] connection dropped ({e}), reconnecting...",
                  flush=True)
            await asyncio.sleep(2)
