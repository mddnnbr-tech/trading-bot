"""
breakout_agent.py
-----------------
Identifies stocks breaking out above key resistance levels with volume confirmation.

Strategy:
  - Detects price breaking above 20-day, 50-day, or 52-week highs
  - Requires expanding volume (volume spike ≥ 1.5x 20-day average)
  - ATR filter: breakout candle must be meaningful (> 1 ATR)
  - Consolidation check: price was range-bound before the break

Best in: BREAKOUT, BULL_TREND regimes
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("BreakoutAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "NVDA", "AAPL", "MSFT", "AMZN", "META",
    "GOOGL", "TSLA", "AMD", "SMCI", "AVGO",
    "SPY", "QQQ", "IWM", "XLK", "PLTR",
    "CRWD", "PANW", "SNOW", "MDB", "DDOG",
]

VOLUME_SPIKE_MIN  = 1.5    # volume must be 1.5x the 20-day avg
ATR_PERIOD        = 14
CONSOL_DAYS       = 10     # look for consolidation in prior N days
CONSOL_RANGE_PCT  = 0.05   # price range < 5% = consolidation
STOP_LOSS_ATR     = 1.5    # stop loss = 1.5 ATR below breakout
TARGET_RR         = 2.5    # reward/risk ratio for target
MIN_CONFIDENCE    = 0.55
EXPIRY_DAYS       = 14


class BreakoutAgent:
    name = "BreakoutAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
        self.regime_affinity = ["BREAKOUT", "BULL_TREND"]

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
                log.debug(f"BreakoutAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        df = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
        if df is None or len(df) < 60:
            return None

        close  = df["Close"]
        high   = df["High"]
        low    = df["Low"]
        volume = df["Volume"]
        price  = float(close.iloc[-1])

        # Current volume vs 20-day average
        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        vol_now = float(volume.iloc[-1])
        vol_spike = vol_now / vol_avg if vol_avg > 0 else 0

        if vol_spike < VOLUME_SPIKE_MIN:
            return None

        # ATR
        atr = self._atr(high, low, close, ATR_PERIOD)

        # Resistance levels to test against
        high_20  = float(close.iloc[-22:-1].max())  # exclude today
        high_50  = float(close.iloc[-52:-1].max())
        high_52w = float(close.iloc[:-1].max())

        # Breakout factors
        factors = []
        breakout_level = None

        if price > high_20 and close.iloc[-2] <= high_20:
            factors.append(f"Breakout above 20d high ${high_20:.2f}")
            breakout_level = high_20

        if price > high_50 and close.iloc[-2] <= high_50:
            factors.append(f"Breakout above 50d high ${high_50:.2f}")
            breakout_level = high_50

        if price >= high_52w * 0.995:  # within 0.5% of 52-week high
            factors.append(f"At/near 52-week high ${high_52w:.2f}")

        if not factors:
            return None

        factors.append(f"Volume spike {vol_spike:.1f}x avg")

        # Consolidation check: was price range-bound before breakout?
        consol_window = close.iloc[-(CONSOL_DAYS + 2):-1]
        consol_range  = float(consol_window.max() - consol_window.min())
        consol_pct    = consol_range / float(consol_window.mean()) if float(consol_window.mean()) > 0 else 1
        if consol_pct < CONSOL_RANGE_PCT:
            factors.append(f"Prior consolidation ({consol_pct*100:.1f}% range)")

        # Candle size check (today's move > 0.5 ATR)
        today_move = price - float(close.iloc[-2])
        if atr > 0 and today_move > 0.5 * atr:
            factors.append(f"Strong candle ({today_move/atr:.1f}x ATR)")

        n = len(factors)
        if n < 2:
            return None

        conf_map = {2: 0.58, 3: 0.67, 4: 0.76, 5: 0.83}
        confidence = conf_map.get(n, 0.87 if n >= 6 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

        stop   = round(price - (STOP_LOSS_ATR * atr), 2) if atr > 0 else round(price * 0.975, 2)
        risk   = price - stop
        target = round(price + risk * TARGET_RR, 2)

        return {
            "agent":           self.name,
            "strategy":        "single_leg_calls",
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       "long",
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
    def _atr(high, low, close, period: int) -> float:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = BreakoutAgent(watchlist=["NVDA", "AAPL", "MSFT"])
    sigs  = agent.generate_signals()
    print(f"BreakoutAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
