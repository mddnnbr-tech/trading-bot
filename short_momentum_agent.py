"""
short_momentum_agent.py
-----------------------
Generates SHORT signals for stocks with strong downward momentum.

Strategy:
  - Negative rate-of-change across multiple timeframes
  - Volume on down days > volume on up days (distribution)
  - Price below key moving averages
  - Relative weakness vs SPY (underperforming the market)
  - Sector weakness (ETF momentum negative)

Best in: BEAR_TREND, HIGH_VOL regimes
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("ShortMomentumAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "TSLA", "RIVN", "LCID", "NKLA", "ARKK",
    "MARA", "RIOT", "COIN", "HOOD", "SOFI",
    "QQQ", "SPY", "IWM", "XLF", "XLE",
    "NFLX", "DIS", "BA", "WBA", "CVS",
]

MAX_ROC_5D       = -1.5   # must be down at least 1.5% in 5 days
MAX_RS           = 0.95   # must underperform SPY
SMA_PERIOD       = 20
MIN_CONFIDENCE   = 0.55
STOP_LOSS_PCT    = 0.025
TARGET_PCT       = 0.06
EXPIRY_DAYS      = 14


class ShortMomentumAgent:
    name = "ShortMomentumAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
        self.regime_affinity = ["BEAR_TREND", "HIGH_VOL"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            return []

        try:
            spy = yf.Ticker("SPY").history(period="2mo", interval="1d", auto_adjust=True)
            spy_roc_5 = float((spy["Close"].iloc[-1] / spy["Close"].iloc[-6] - 1) * 100) if len(spy) >= 6 else 0
        except Exception:
            spy = None
            spy_roc_5 = 0.0

        signals = []
        for symbol in self.watchlist:
            if symbol in ("SPY", "QQQ"):
                continue
            try:
                sig = self._analyze(symbol, spy_roc_5)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.debug(f"ShortMomentumAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str, spy_roc_5: float) -> Optional[dict]:
        df = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=True)
        if df is None or len(df) < 25:
            return None

        close  = df["Close"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        roc_5  = float((close.iloc[-1] / close.iloc[-6]  - 1) * 100) if len(close) >= 6  else 0
        roc_10 = float((close.iloc[-1] / close.iloc[-11] - 1) * 100) if len(close) >= 11 else 0
        roc_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0

        # Must have negative 5d momentum
        if roc_5 > MAX_ROC_5D:
            return None

        # Relative weakness vs SPY
        rs = roc_5 / spy_roc_5 if spy_roc_5 < 0 else 0
        if spy_roc_5 < 0 and rs < MAX_RS:
            pass  # underperforming even a falling market
        elif spy_roc_5 >= 0 and roc_5 < -2.0:
            pass  # falling while market is up = very weak
        else:
            return None

        # Price below SMA20
        sma20 = float(close.rolling(SMA_PERIOD).mean().iloc[-1])
        if price > sma20:
            return None

        # Volume: down days should have higher volume (distribution)
        recent = df.tail(10)
        up_vol   = recent[recent["Close"] > recent["Close"].shift(1)]["Volume"].mean()
        down_vol = recent[recent["Close"] < recent["Close"].shift(1)]["Volume"].mean()
        vol_bearish = down_vol > up_vol if (not pd.isna(up_vol) and not pd.isna(down_vol)) else False

        factors = []
        if roc_5  < MAX_ROC_5D:     factors.append(f"5d ROC {roc_5:.1f}%")
        if roc_10 < -3.0:            factors.append(f"10d ROC {roc_10:.1f}%")
        if roc_20 < -6.0:            factors.append(f"20d ROC {roc_20:.1f}%")
        if spy_roc_5 >= 0 and roc_5 < -2.0:
            factors.append("Falling vs rising market (very weak)")
        if vol_bearish:              factors.append("Vol: down days dominate (distribution)")
        if price < sma20 * 0.98:     factors.append(f"Price ${price:.2f} well below SMA20 ${sma20:.2f}")

        n = len(factors)
        if n < 2:
            return None

        conf_map = {2: 0.57, 3: 0.66, 4: 0.74, 5: 0.81}
        confidence = conf_map.get(n, 0.84 if n >= 6 else 0.55)

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


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = ShortMomentumAgent(watchlist=["TSLA", "RIVN", "ARKK"])
    sigs  = agent.generate_signals()
    print(f"ShortMomentumAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
