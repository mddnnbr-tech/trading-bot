"""
premarket_agent.py
------------------
Trades pre-market gaps and early morning setups.

Strategies:
  1. Gap-and-go: strong pre-market gap (>2%) with volume → trade the gap direction
     on open if the first 5 minutes confirm the move.
  2. Gap-fill: if a stock gaps but quickly reverses, fade the gap.
  3. Pre-market catalyst: volume spike in pre-market on news = intraday trade.

Data: yfinance pre-market data (limited) + today's open vs yesterday's close.

Note: Best signal quality is before 10:30 AM ET. The scheduler runs this
agent every minute so it will fire during the best pre-market window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo
from typing import Optional

log = logging.getLogger("PremarketAgent")

ET = ZoneInfo("America/New_York")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA",
    "TSLA", "META", "AMZN", "GOOGL", "AMD",
    "NFLX", "COIN", "PLTR", "SOFI", "HOOD",
]

GAP_LONG_PCT    = 1.5    # gap up ≥ 1.5% = consider long
GAP_SHORT_PCT   = -1.5   # gap down ≤ -1.5% = consider short
FADE_THRESHOLD  = 0.50   # gap retraced ≥ 50% in first bar = fade signal
PREMARKET_CLOSE = time(9, 45)  # only valid before 9:45 AM ET
STOP_LOSS_PCT   = 0.02
TARGET_PCT      = 0.04   # shorter target for day trades
MIN_CONFIDENCE  = 0.55
EXPIRY_DAYS     = 1      # same-day or next-day expiry for gap trades


class PremarketAgent:
    name = "PremarketAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL", "BREAKOUT", "NEUTRAL"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            return []

        # Only relevant in the first 30 minutes of the session
        now_et = datetime.now(ET).time()
        if now_et > PREMARKET_CLOSE:
            return []

        signals = []
        for symbol in self.watchlist:
            try:
                sig = self._analyze(symbol)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"PremarketAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        # Get 5-minute intraday data for today
        ticker  = yf.Ticker(symbol)
        intra   = ticker.history(period="5d", interval="5m", auto_adjust=True)
        daily   = ticker.history(period="5d", interval="1d", auto_adjust=True)

        if intra is None or intra.empty or daily is None or daily.empty:
            return None

        # Localize if needed
        if intra.index.tz is None:
            intra.index = intra.index.tz_localize("UTC").tz_convert(ET)
        else:
            intra.index = intra.index.tz_convert(ET)

        today = datetime.now(ET).date()
        today_bars = intra[intra.index.date == today]

        if today_bars.empty:
            return None

        # Yesterday's close
        prev_close_rows = daily[daily.index.normalize().date < today]
        if prev_close_rows.empty:
            return None
        prev_close = float(prev_close_rows["Close"].iloc[-1])

        # Today's open (first bar)
        open_price  = float(today_bars["Open"].iloc[0])
        curr_price  = float(today_bars["Close"].iloc[-1])
        vol_today   = float(today_bars["Volume"].sum())
        vol_avg_day = float(daily["Volume"].iloc[:-1].mean()) if len(daily) > 1 else vol_today

        gap_pct = (open_price - prev_close) / prev_close * 100
        factors  = []
        direction = None

        # Gap-and-go: gap holds direction through first bar(s)
        if gap_pct >= GAP_LONG_PCT:
            if curr_price >= open_price * 0.995:  # price holding near open = momentum
                factors.append(f"Gap up {gap_pct:.1f}% from ${prev_close:.2f} → ${open_price:.2f}")
                factors.append("Gap holding (no fill) → momentum")
                direction = "long"
            elif curr_price <= open_price * 0.99:  # gap filling fast = fade
                pct_filled = (open_price - curr_price) / (open_price - prev_close)
                if pct_filled >= FADE_THRESHOLD:
                    factors.append(f"Gap up {gap_pct:.1f}% reversing ({pct_filled*100:.0f}% filled) → fade")
                    direction = "short"

        elif gap_pct <= GAP_SHORT_PCT:
            if curr_price <= open_price * 1.005:  # gap down holding
                factors.append(f"Gap down {gap_pct:.1f}% from ${prev_close:.2f} → ${open_price:.2f}")
                factors.append("Gap holding → continuation")
                direction = "short"
            elif curr_price >= open_price * 1.01:  # gap filling = fade to long
                pct_filled = (curr_price - open_price) / (prev_close - open_price)
                if pct_filled >= FADE_THRESHOLD:
                    factors.append(f"Gap down {gap_pct:.1f}% reversing ({pct_filled*100:.0f}% filled) → fade")
                    direction = "long"

        if not factors or direction is None:
            return None

        # Volume confirmation
        if vol_avg_day > 0:
            vol_ratio = vol_today / (vol_avg_day / 13)  # 13 × 30min = full day
            if vol_ratio >= 1.5:
                factors.append(f"Above-average early volume ({vol_ratio:.1f}x)")

        n = len(factors)
        conf_map = {1: 0.58, 2: 0.66, 3: 0.74}
        confidence = conf_map.get(n, 0.78 if n >= 4 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

        price = curr_price
        if direction == "long":
            stop   = round(price * (1 - STOP_LOSS_PCT), 2)
            target = round(price * (1 + TARGET_PCT), 2)
            strat  = "single_leg_calls"
        else:
            stop   = round(price * (1 + STOP_LOSS_PCT), 2)
            target = round(price * (1 - TARGET_PCT), 2)
            strat  = "single_leg_puts"

        return {
            "agent":           self.name,
            "strategy":        strat,
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       direction,
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


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=max(days_out, 1))
    days_to_friday = (4 - target.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = PremarketAgent(watchlist=["SPY", "AAPL", "NVDA"])
    sigs  = agent.generate_signals()
    print(f"PremarketAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
