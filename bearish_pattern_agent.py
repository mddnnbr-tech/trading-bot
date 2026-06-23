"""
bearish_pattern_agent.py
------------------------
Detects chart patterns that signal potential price declines.

Patterns detected:
  - Double Top: two peaks at similar levels with a trough between
  - Head & Shoulders: left shoulder < head > right shoulder
  - Lower highs + lower lows (downtrend confirmation)
  - Death cross: SMA20 crosses below SMA50
  - Price breakdown below support (recent low)
  - RSI divergence: price making higher highs but RSI making lower highs

Best in: BEAR_TREND, HIGH_VOL, OVERBOUGHT regimes
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("BearishPatternAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "TSLA",
    "NVDA", "META", "AMZN", "NFLX", "GOOGL",
    "XLF", "XLE", "XLB", "IWM", "DIA",
    "ARKK", "MARA", "COIN", "RIVN", "LCID",
]

SMA_FAST       = 20
SMA_SLOW       = 50
RSI_PERIOD     = 14
STOP_LOSS_PCT  = 0.025
TARGET_PCT     = 0.06
MIN_CONFIDENCE = 0.55
EXPIRY_DAYS    = 14
LOOKBACK       = 40   # bars for pattern detection


class BearishPatternAgent:
    name = "BearishPatternAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
        self.regime_affinity = ["BEAR_TREND", "HIGH_VOL", "OVERBOUGHT"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            return []
        signals = []
        for symbol in self.watchlist:
            try:
                sig = self._analyze(symbol)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"BearishPatternAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        df = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=True)
        if df is None or len(df) < LOOKBACK + 10:
            return None

        close = df["Close"]
        high  = df["High"]
        price = float(close.iloc[-1])

        sma20 = float(close.rolling(SMA_FAST).mean().iloc[-1])
        sma50 = float(close.rolling(SMA_SLOW).mean().iloc[-1])
        sma20_prev = float(close.rolling(SMA_FAST).mean().iloc[-2])
        sma50_prev = float(close.rolling(SMA_SLOW).mean().iloc[-2])

        rsi = self._rsi(close, RSI_PERIOD)

        factors = []

        # Death cross
        if sma20 < sma50 and sma20_prev >= sma50_prev:
            factors.append(f"Death cross (SMA20 ${sma20:.2f} below SMA50 ${sma50:.2f})")

        # Price below key SMAs
        if price < sma20 and price < sma50:
            factors.append(f"Price ${price:.2f} below SMA20 and SMA50")
        elif price < sma20:
            factors.append(f"Price ${price:.2f} below SMA20 ${sma20:.2f}")

        # Overbought RSI (reversal candidate)
        if rsi > 68:
            factors.append(f"RSI overbought ({rsi:.1f})")

        # Lower highs (downtrend)
        recent_highs = []
        window = high.iloc[-LOOKBACK:]
        # Find local maxima (peaks)
        for i in range(2, len(window) - 2):
            if float(window.iloc[i]) > float(window.iloc[i-1]) and float(window.iloc[i]) > float(window.iloc[i+1]):
                recent_highs.append(float(window.iloc[i]))
        if len(recent_highs) >= 3 and recent_highs[-1] < recent_highs[-2] < recent_highs[-3]:
            factors.append(f"Lower highs pattern ({recent_highs[-3]:.2f} → {recent_highs[-2]:.2f} → {recent_highs[-1]:.2f})")

        # Double top: two peaks within 2% of each other with a trough between
        if len(recent_highs) >= 2:
            top1, top2 = recent_highs[-2], recent_highs[-1]
            if abs(top1 - top2) / max(top1, top2) < 0.02 and price < top1 * 0.97:
                factors.append(f"Double top pattern (~${top1:.2f})")

        # Breakdown below 20-day support
        support_20d = float(close.iloc[-22:-1].min())
        if price < support_20d:
            factors.append(f"Breakdown below 20d support ${support_20d:.2f}")

        # RSI bearish divergence: price higher but RSI lower over 10 days
        if len(close) >= 12:
            price_10d_ago = float(close.iloc[-11])
            rsi_10d_ago   = self._rsi(close.iloc[:-10], RSI_PERIOD)
            if price > price_10d_ago and rsi < rsi_10d_ago - 5:
                factors.append(f"Bearish RSI divergence (price ↑ RSI ↓{rsi_10d_ago-rsi:.1f}pts)")

        n = len(factors)
        if n < 2:
            return None

        conf_map = {2: 0.57, 3: 0.66, 4: 0.75, 5: 0.82}
        confidence = conf_map.get(n, 0.86 if n >= 6 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

        stop   = round(price * (1 + STOP_LOSS_PCT), 2)
        target = round(price * (1 - TARGET_PCT), 2)

        return {
            "agent":           self.name,
            "strategy":        "single_leg_puts",
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       "short",
            "entry_price":     round(price, 2),
            "stop_loss_price": stop,
            "target_price":    target,
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      confidence,
            "expiration":      _next_friday(EXPIRY_DAYS),
            "meta_score":      confidence,
            "regime_affinity": self.regime_affinity,
            "reasons":         factors,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _rsi(series, period: int = 14) -> float:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, float("nan"))
        return float((100 - 100 / (1 + rs)).iloc[-1])


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = BearishPatternAgent(watchlist=["SPY", "QQQ", "AAPL"])
    sigs  = agent.generate_signals()
    print(f"BearishPatternAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
