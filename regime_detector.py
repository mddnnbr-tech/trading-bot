"""
regime_detector.py
------------------
Identifies the current market regime by analysing SPY, VIX, and sector breadth.
Returns a set of active regime strings consumed by MetaAgent for agent weighting.

Regimes:
  BULL_TREND    - SPY above both SMAs, low VIX, strong breadth
  BEAR_TREND    - SPY below both SMAs, elevated VIX
  HIGH_VOL      - VIX > 25, large intraday ranges
  LOW_VOL       - VIX < 15, tight ranges
  BREAKOUT      - Price near 52-week high with expanding volume
  OVERSOLD      - RSI < 35 on SPY (potential bounce setup)
  OVERBOUGHT    - RSI > 70 on SPY (potential fade setup)
  NEUTRAL       - None of the above (default)

Multiple regimes can be active simultaneously (e.g. BULL_TREND + LOW_VOL).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Set

log = logging.getLogger("RegimeDetector")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

# Thresholds
VIX_HIGH        = 25.0
VIX_LOW         = 15.0
RSI_PERIOD      = 14
RSI_OVERSOLD    = 35.0
RSI_OVERBOUGHT  = 70.0
SMA_FAST        = 20
SMA_SLOW        = 50
PCT_FROM_HIGH   = 0.03   # within 3% of 52-week high = breakout candidate


class RegimeDetector:
    """Detects the current market regime. Called once per ensemble cycle."""

    name = "RegimeDetector"

    def detect(self) -> Set[str]:
        """Return a set of active regime labels."""
        if not _YF_OK:
            log.warning("yfinance not available — defaulting to NEUTRAL regime")
            return {"NEUTRAL"}

        try:
            spy_df = self._fetch("SPY", period="1y", interval="1d")
            vix_df = self._fetch("^VIX", period="5d", interval="1d")

            if spy_df is None or spy_df.empty:
                return {"NEUTRAL"}

            regimes: Set[str] = set()

            spy_close = spy_df["Close"]
            sma_fast  = float(spy_close.rolling(SMA_FAST).mean().iloc[-1])
            sma_slow  = float(spy_close.rolling(SMA_SLOW).mean().iloc[-1])
            price     = float(spy_close.iloc[-1])
            high_52w  = float(spy_close.tail(252).max())
            vol_sma   = float(spy_df["Volume"].rolling(20).mean().iloc[-1]) if "Volume" in spy_df else None
            vol_now   = float(spy_df["Volume"].iloc[-1]) if "Volume" in spy_df else None

            rsi = self._rsi(spy_close, RSI_PERIOD)

            # VIX level
            vix = None
            if vix_df is not None and not vix_df.empty:
                vix = float(vix_df["Close"].iloc[-1])

            # Trend
            if price > sma_fast > sma_slow:
                regimes.add("BULL_TREND")
            elif price < sma_fast < sma_slow:
                regimes.add("BEAR_TREND")

            # Volatility
            if vix is not None:
                if vix > VIX_HIGH:
                    regimes.add("HIGH_VOL")
                elif vix < VIX_LOW:
                    regimes.add("LOW_VOL")

            # RSI extremes
            if rsi < RSI_OVERSOLD:
                regimes.add("OVERSOLD")
            elif rsi > RSI_OVERBOUGHT:
                regimes.add("OVERBOUGHT")

            # Breakout (near 52-week high + expanding volume)
            if price >= high_52w * (1 - PCT_FROM_HIGH):
                if vol_sma and vol_now and vol_now > vol_sma * 1.2:
                    regimes.add("BREAKOUT")

            if not regimes:
                regimes.add("NEUTRAL")

            log.info(
                f"RegimeDetector: SPY=${price:.2f} RSI={rsi:.1f}"
                f"{' VIX=' + str(round(vix, 1)) if vix else ''}"
                f" → {regimes}"
            )
            return regimes

        except Exception as e:
            log.warning(f"RegimeDetector error: {e} — defaulting to NEUTRAL")
            return {"NEUTRAL"}

    @staticmethod
    def _fetch(symbol: str, period: str, interval: str):
        try:
            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
            return df if not df.empty else None
        except Exception:
            return None

    @staticmethod
    def _rsi(series, period: int = 14) -> float:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = 100 - 100 / (1 + rs)
        return float(rsi.iloc[-1])


if __name__ == "__main__":
    rd = RegimeDetector()
    print("Active regimes:", rd.detect())
