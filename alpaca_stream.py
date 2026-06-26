"""
alpaca_stream.py
----------------
Real-time market data from Alpaca WebSocket streaming API.

Provides a shared in-memory price cache that all agents can query instead of
polling yfinance every minute. This enables catching intraday surges, volume
spikes, and fast-moving opportunities that daily/5-minute yfinance data misses.

Usage:
  from alpaca_stream import get_latest_price, get_recent_bars, is_streaming

  # In any agent:
  price = get_latest_price("NVDA")  # returns real-time price or None
  bars  = get_recent_bars("NVDA", n=20)  # returns list of recent OHLCV dicts

Requirements:
  pip install alpaca-py
  Set in .env:
    ALPACA_API_KEY=your_key
    ALPACA_API_SECRET=your_secret
    ALPACA_PAPER=true   # use paper trading data endpoint

Free Alpaca account: https://alpaca.markets
- Free tier provides real-time IEX data (sufficient for paper trading)
- Upgrade to subscription for SIP (consolidated tape) data

Architecture:
  - AlpacaStream runs a background thread with a WebSocket connection
  - On each trade/bar update, it updates the shared _cache dict
  - Agents read from _cache via get_latest_price() / get_recent_bars()
  - If streaming is unavailable, agents fall back to yfinance automatically
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("AlpacaStream")

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# Symbols to stream — union of all agent watchlists
DEFAULT_SYMBOLS = [
    # Major ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLU", "XLP",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    # High-momentum / high-volume
    "PLTR", "COIN", "SOFI", "SMCI", "AVGO", "CRWD", "PANW", "SNOW",
    # Broad market movers
    "NFLX", "UBER", "ABNB", "SHOP", "SQ", "RBLX",
    # Macro hedges
    "GLD", "TLT", "UUP", "VIX",
]

MAX_BARS_PER_SYMBOL = 100  # keep last N 1-minute bars in memory

# Shared in-memory cache
_lock  = threading.Lock()
_cache: dict[str, dict] = {}   # symbol → {price, bid, ask, volume, timestamp}
_bars:  dict[str, deque] = {}  # symbol → deque of bar dicts

_stream_thread: Optional[threading.Thread] = None
_stream_active = False


# ── Public API ────────────────────────────────────────────────────────────────

def get_latest_price(symbol: str) -> Optional[float]:
    """Return the most recent trade price for symbol, or None if not cached."""
    with _lock:
        entry = _cache.get(symbol)
        return entry["price"] if entry else None


def get_latest_quote(symbol: str) -> Optional[dict]:
    """Return {bid, ask, mid} or None."""
    with _lock:
        entry = _cache.get(symbol)
        if not entry or "bid" not in entry:
            return None
        bid = entry.get("bid", 0)
        ask = entry.get("ask", 0)
        return {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4)}


def get_recent_bars(symbol: str, n: int = 20) -> list[dict]:
    """Return the last n 1-minute OHLCV bars for symbol (newest last)."""
    with _lock:
        dq = _bars.get(symbol)
        if not dq:
            return []
        return list(dq)[-n:]


def get_volume_spike(symbol: str, multiplier: float = 2.0) -> bool:
    """Return True if the most recent bar's volume is > multiplier × 20-bar avg."""
    bars = get_recent_bars(symbol, 21)
    if len(bars) < 5:
        return False
    recent_vol   = bars[-1].get("volume", 0)
    avg_vol      = sum(b.get("volume", 0) for b in bars[:-1]) / max(len(bars) - 1, 1)
    return avg_vol > 0 and recent_vol > avg_vol * multiplier


def is_streaming() -> bool:
    """True if the WebSocket connection is active."""
    return _stream_active


def start(symbols: list[str] | None = None) -> bool:
    """
    Start the Alpaca streaming thread. Call once at process startup.
    Returns True if streaming started, False if credentials missing.
    """
    global _stream_thread, _stream_active

    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        log.warning(
            "AlpacaStream: ALPACA_API_KEY / ALPACA_API_SECRET not set in .env — "
            "real-time streaming disabled. Agents will use yfinance fallback."
        )
        return False

    if _stream_active:
        log.info("AlpacaStream: already running")
        return True

    watch = symbols or DEFAULT_SYMBOLS

    _stream_thread = threading.Thread(
        target=_stream_loop,
        args=(watch,),
        daemon=True,
        name="AlpacaStreamThread",
    )
    _stream_thread.start()
    log.info(f"AlpacaStream: started — subscribing to {len(watch)} symbols")
    return True


def stop():
    global _stream_active
    _stream_active = False
    log.info("AlpacaStream: stop requested")


# ── Streaming loop ────────────────────────────────────────────────────────────

def _stream_loop(symbols: list[str]):
    """Background thread: maintains WebSocket connection with auto-reconnect."""
    global _stream_active

    try:
        from alpaca.data.live import StockDataStream  # type: ignore
        from alpaca.data.enums import DataFeed         # type: ignore
    except ImportError:
        log.error(
            "alpaca-py not installed. Run: pip install alpaca-py\n"
            "Agents will fall back to yfinance data."
        )
        return

    _stream_active = True
    backoff = 5

    while _stream_active:
        try:
            log.info(f"AlpacaStream: connecting (paper={ALPACA_PAPER})...")
            feed = DataFeed.IEX if ALPACA_PAPER else DataFeed.SIP

            stream = StockDataStream(
                api_key=ALPACA_API_KEY,
                secret_key=ALPACA_API_SECRET,
                feed=feed,
            )

            async def handle_trade(trade):
                sym   = trade.symbol
                price = float(trade.price)
                ts    = datetime.now(timezone.utc).isoformat()
                with _lock:
                    prev = _cache.get(sym, {})
                    _cache[sym] = {
                        **prev,
                        "price":     price,
                        "volume":    float(trade.size),
                        "timestamp": ts,
                    }

            async def handle_bar(bar):
                sym = bar.symbol
                b   = {
                    "open":      float(bar.open),
                    "high":      float(bar.high),
                    "low":       float(bar.low),
                    "close":     float(bar.close),
                    "volume":    float(bar.volume),
                    "vwap":      float(bar.vwap) if hasattr(bar, "vwap") else None,
                    "timestamp": bar.timestamp.isoformat() if hasattr(bar.timestamp, "isoformat") else str(bar.timestamp),
                }
                with _lock:
                    if sym not in _bars:
                        _bars[sym] = deque(maxlen=MAX_BARS_PER_SYMBOL)
                    _bars[sym].append(b)
                    # Update latest price from bar close
                    prev = _cache.get(sym, {})
                    _cache[sym] = {**prev, "price": b["close"], "timestamp": b["timestamp"]}

            async def handle_quote(quote):
                sym = quote.symbol
                with _lock:
                    prev = _cache.get(sym, {})
                    _cache[sym] = {
                        **prev,
                        "bid": float(quote.bid_price),
                        "ask": float(quote.ask_price),
                    }

            stream.subscribe_trades(handle_trade, *symbols)
            stream.subscribe_bars(handle_bar, *symbols)
            stream.subscribe_quotes(handle_quote, *symbols)

            log.info("AlpacaStream: connected — receiving live data")
            backoff = 5  # reset on successful connect
            stream.run()  # blocks until disconnect

        except Exception as e:
            if _stream_active:
                log.warning(f"AlpacaStream: connection error ({e}), retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)

    log.info("AlpacaStream: stopped")


# ── Surge/drop detection (called by market_scheduler or agents) ───────────────

def detect_surges(threshold_pct: float = 3.0) -> list[dict]:
    """
    Return list of symbols with a recent 1-minute bar that moved ≥ threshold_pct.
    Used by agents to catch real-time breakouts and drops.
    """
    surges = []
    with _lock:
        for symbol, dq in _bars.items():
            if len(dq) < 2:
                continue
            prev_close = dq[-2].get("close", 0)
            curr_close = dq[-1].get("close", 0)
            if prev_close <= 0:
                continue
            pct = (curr_close - prev_close) / prev_close * 100
            if abs(pct) >= threshold_pct:
                surges.append({
                    "symbol":    symbol,
                    "pct_move":  round(pct, 2),
                    "direction": "up" if pct > 0 else "down",
                    "price":     curr_close,
                    "volume":    dq[-1].get("volume", 0),
                })
    return sorted(surges, key=lambda x: abs(x["pct_move"]), reverse=True)


# ── Quick status check ────────────────────────────────────────────────────────

def status() -> dict:
    with _lock:
        return {
            "streaming":      _stream_active,
            "symbols_cached": len(_cache),
            "symbols_bars":   len(_bars),
            "sample_prices":  {k: v.get("price") for k, v in list(_cache.items())[:5]},
        }


if __name__ == "__main__":
    import time as t
    print("Starting Alpaca stream test (Ctrl+C to stop)...")
    started = start(["SPY", "AAPL", "NVDA"])
    if not started:
        print("Cannot start: check ALPACA_API_KEY and ALPACA_API_SECRET in .env")
    else:
        for _ in range(30):
            t.sleep(2)
            print(status())
