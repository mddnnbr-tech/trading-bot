"""
earnings_agent.py
-----------------
Trades earnings catalyst events — both pre-earnings run-ups and post-earnings gaps.

Strategies:
  1. Pre-earnings run-up: buy calls 3-5 days before expected earnings
     if the stock has a history of running up before earnings.
  2. Post-earnings gap play: on the day of/after earnings, trade the gap
     direction if the move is confirmed by volume.
  3. Earnings surprise fade: short a stock that gapped up on earnings if
     it fails to hold the open (gap-and-crap).

Data source: yfinance earnings calendar + EarningsWhispers-compatible approach.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

log = logging.getLogger("EarningsAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

# Stocks with earnings typically within the next 10 trading days
# In production this list should be fetched from an earnings calendar API
EARNINGS_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "META", "AMZN",
    "GOOGL", "TSLA", "AMD", "NFLX", "QCOM",
    "INTC", "MU", "AMAT", "LRCX", "KLAC",
    "CRM", "ORCL", "SAP", "ADBE", "NOW",
    "JPM", "GS", "MS", "BAC", "WFC",
]

PRE_EARNINGS_DAYS   = 5      # look for run-up setup N days before earnings
GAP_THRESHOLD_PCT   = 3.0    # minimum gap % to trade post-earnings
GAP_CONFIRM_VOL     = 2.0    # volume must be 2x average on gap day
STOP_LOSS_PCT       = 0.03
TARGET_PCT          = 0.07
MIN_CONFIDENCE      = 0.55
EXPIRY_DAYS         = 7      # shorter expiry for earnings plays


class EarningsAgent:
    name = "EarningsAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or EARNINGS_WATCHLIST
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL", "NEUTRAL"]

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
                log.debug(f"EarningsAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        ticker = yf.Ticker(symbol)

        # Check for upcoming or recent earnings
        earnings_date = self._get_earnings_date(ticker)
        df = ticker.history(period="3mo", interval="1d", auto_adjust=True)

        if df is None or df.empty:
            return None

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        today = date.today()
        factors = []
        direction = None

        if earnings_date:
            days_until = (earnings_date - today).days

            # --- Strategy 1: Pre-earnings run-up ---
            if 1 <= days_until <= PRE_EARNINGS_DAYS:
                # Historical tendency: does this stock run before earnings?
                hist_run = self._pre_earnings_tendency(close)
                if hist_run > 0.5:
                    factors.append(f"Earnings in {days_until}d — historical pre-earnings run {hist_run*100:.0f}% of time")
                    direction = "long"

                # Momentum leading into earnings
                roc_5 = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else 0
                if roc_5 > 1.0:
                    factors.append(f"Positive momentum into earnings ({roc_5:.1f}% 5d)")

            # --- Strategy 2: Post-earnings gap play ---
            elif days_until in (-1, 0) or (days_until < 0 and days_until >= -3):
                # Look for gap today
                if len(df) >= 2:
                    prev_close = float(close.iloc[-2])
                    today_open = float(df["Open"].iloc[-1]) if "Open" in df.columns else price
                    gap_pct    = (today_open - prev_close) / prev_close * 100
                    vol_ratio  = float(volume.iloc[-1]) / float(volume.rolling(20).mean().iloc[-1])

                    if abs(gap_pct) >= GAP_THRESHOLD_PCT and vol_ratio >= GAP_CONFIRM_VOL:
                        gap_dir = "long" if gap_pct > 0 else "short"
                        factors.append(f"Post-earnings gap {gap_pct:+.1f}% on {vol_ratio:.1f}x volume")
                        direction = gap_dir

        if not factors or direction is None:
            return None

        n = len(factors)
        conf_map = {1: 0.58, 2: 0.68, 3: 0.76}
        confidence = conf_map.get(n, 0.80 if n >= 4 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

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

    @staticmethod
    def _get_earnings_date(ticker) -> Optional[date]:
        """Try to get the next earnings date from yfinance calendar."""
        try:
            cal = ticker.calendar
            if cal is None:
                return None
            # calendar can be a DataFrame or dict depending on yfinance version
            if hasattr(cal, "columns"):
                dates = cal.get("Earnings Date")
                if dates is not None and not dates.empty:
                    d = dates.iloc[0]
                    if hasattr(d, "date"):
                        return d.date()
                    return None
            elif isinstance(cal, dict):
                d = cal.get("Earnings Date")
                if d:
                    if isinstance(d, list) and d:
                        d = d[0]
                    if hasattr(d, "date"):
                        return d.date()
        except Exception:
            pass
        return None

    @staticmethod
    def _pre_earnings_tendency(close) -> float:
        """
        Estimate what fraction of the last 4 quarterly earnings windows
        saw the stock rise in the 5 days before earnings.
        Approximation: look at returns in the 5 days before each quarter end.
        """
        if len(close) < 252:
            return 0.5  # not enough history
        wins = 0
        for q in range(4):
            start = -(q * 63 + 10)
            end   = -(q * 63 + 5)
            try:
                ret = float(close.iloc[end] / close.iloc[start] - 1)
                if ret > 0:
                    wins += 1
            except Exception:
                pass
        return wins / 4


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = EarningsAgent(watchlist=["NVDA", "AAPL", "MSFT"])
    sigs  = agent.generate_signals()
    print(f"EarningsAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
