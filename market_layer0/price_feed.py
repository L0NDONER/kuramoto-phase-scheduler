"""price_feed.py — selects a real price stream by source name ('crypto'
or 'stock'), so run_test.py doesn't need to know which specific module
backs "the market" -- binance_feed.py and finnhub_feed.py both implement
the same contract (an async generator yielding one float price per real
trade print), this module is just the switch between them. Refactored
out of a hardcoded binance_feed import so a stock source could be added
without touching run_test.py's actual oscillator/telemetry wiring.
"""
import os

from binance_feed import price_stream as _crypto_price_stream
from finnhub_feed import price_stream as _stock_price_stream

SOURCES = {"crypto", "stock"}


def price_stream(source, symbol):
    """Returns the right async generator for `source` (unstarted --
    nothing connects until the caller iterates it, same as calling
    either backend's price_stream() directly)."""
    if source == "crypto":
        return _crypto_price_stream(symbol)
    if source == "stock":
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            raise SystemExit(
                "[price_feed] FINNHUB_API_KEY not set -- get a free key at "
                "finnhub.io and `export FINNHUB_API_KEY=...` before running "
                "with MARKET_FEED_SOURCE=stock. Same fail-fast-at-startup "
                "shape as reflex_power_cap.py's permission check: better to "
                "stop here than crash on the first real trade."
            )
        return _stock_price_stream(symbol, api_key)
    raise ValueError(f"unknown feed source {source!r}, expected one of {SOURCES}")
