"""
momentum_agent.py
-----------------
Generates LONG signals based on upward price momentum.

Strategy:
  - Rate-of-change (ROC) over multiple timeframes (5d, 10d, 20d)
  - Volume confirmation (expanding volume on up days)
  - Relative strength vs SPY (outperforming the market)
  - Price above short-term moving average

Best in: BULL_TREND, BREAKOUT regimes
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("MomentumAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "NVDA", "MSFT", "AAPL", "META", "AMZN",
    "GOOGL", "TSLA", "AMD", "SMCI", "AVGO",
    "QQQ", "SPY", "XLK", "XLY", "PLTR",
]

ROC_PERIODS      = [5, 10, 20]
MIN_ROC_5D       = 2.0    # at least 2% gain in 5 days
MIN_RS           = 1.05   # 5% stronger than SPY over same period
SMA_PERIOD       = 20
MIN_CONFIDENCE   = 0.55
STOP_LOSS_PCT    = 0.025  # 2.5%
TARGET_PCT       = 0.06   # 6%
EXPIRY_DAYS      = 14


class MomentumAgent:
    name = "MomentumAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
        self.regime_affinity = ["BULL_TREND", "BREAKOUT"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            log.warning("MomentumAgent: yfinance unavailable")
            return []

        try:
            # Use intraday to get current-session SPY performance
            spy = yf.Ticker("SPY").history(period="5d", interval="5m", auto_adjust=True)
            if spy is None or len(spy) < 10:
                spy = yf.Ticker("SPY").history(period="2mo", interval="1d", auto_adjust=True)
            spy_roc_5 = float((spy["Close"].iloc[-1] / spy["Close"].iloc[-6] - 1) * 100) if len(spy) >= 6 else 0
        except Exception:
            spy = None
            spy_roc_5 = 0.0

        signals = []
        for symbol in self.watchlist:
            if symbol == "SPY":
                continue
            try:
                sig = self._analyze(symbol, spy, spy_roc_5)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"MomentumAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str, spy_df, spy_roc_5: float) -> Optional[dict]:
        # Fetch intraday first for current-session momentum
        df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True)
        if df is None or len(df) < 30:
            df = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=True)
        if df is None or len(df) < 25:
            return None

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        # Rate of change
        roc_5  = float((close.iloc[-1] / close.iloc[-6]  - 1) * 100) if len(close) >= 6  else 0
        roc_10 = float((close.iloc[-1] / close.iloc[-11] - 1) * 100) if len(close) >= 11 else 0
        roc_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0

        # Must have positive 5d momentum above threshold
        if roc_5 < MIN_ROC_5D:
            return None

        # Must outperform SPY (relative strength)
        rs = roc_5 / spy_roc_5 if spy_roc_5 > 0 else 0
        if rs < MIN_RS and spy_roc_5 > 0:
            return None

        # Price must be above SMA20
        sma20 = float(close.rolling(SMA_PERIOD).mean().iloc[-1])
        if price < sma20:
            return None

        # Volume confirmation: average volume on up days > average on down days
        recent = df.tail(10)
        up_vol   = recent[recent["Close"] > recent["Close"].shift(1)]["Volume"].mean()
        down_vol = recent[recent["Close"] < recent["Close"].shift(1)]["Volume"].mean()
        vol_bullish = up_vol > down_vol if (not pd.isna(up_vol) and not pd.isna(down_vol)) else False

        # Score: count positive factors
        factors = []
        if roc_5  > MIN_ROC_5D:    factors.append(f"5d ROC +{roc_5:.1f}%")
        if roc_10 > 4.0:            factors.append(f"10d ROC +{roc_10:.1f}%")
        if roc_20 > 8.0:            factors.append(f"20d ROC +{roc_20:.1f}%")
        if rs > MIN_RS:             factors.append(f"RS vs SPY {rs:.2f}x")
        if vol_bullish:             factors.append("Vol: up days dominate")
        if price > sma20 * 1.02:   factors.append(f"Price above SMA20 +{((price/sma20)-1)*100:.1f}%")

        n = len(factors)
        if n < 2:
            return None

        conf_map = {2: 0.58, 3: 0.67, 4: 0.75, 5: 0.82}
        confidence = conf_map.get(n, 0.85 if n >= 6 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

        stop  = round(price * (1 - STOP_LOSS_PCT), 2)
        tgt   = round(price * (1 + TARGET_PCT), 2)
        expiry = _next_friday(EXPIRY_DAYS)

        return {
            "agent":           self.name,
            "strategy":        "single_leg_calls",
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       "long",
            "entry_price":     round(price, 2),
            "stop_loss_price": stop,
            "target_price":    tgt,
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      confidence,
            "expiration":      expiry,
            "meta_score":      confidence,
            "regime_affinity": self.regime_affinity,
            "reasons":         factors,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }


def _next_friday(days_out: int) -> str:
    from datetime import timedelta
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = MomentumAgent(watchlist=["NVDA", "MSFT", "AAPL"])
    sigs  = agent.generate_signals()
    print(f"MomentumAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
