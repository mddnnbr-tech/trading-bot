"""
volatility_agent.py
────────────────────
Volatility mean-reversion specialist — deliberately NOT a directional bet.

Rationale: the ensemble's existing directional agents (MacroAgent,
BearishPatternAgent) have shown repeated wrong-direction losses betting on
"the market will keep going this way." This agent instead trades statistical
overextension: when price stretches far beyond its recent volatility envelope
AND momentum is decelerating (not accelerating), it bets on reversion back
toward the mean. This is a well-documented, statistically-grounded edge
distinct from guessing macro direction.

Signal logic:
  1. Bollinger %B — how far price sits outside its 20-period volatility band
  2. RSI extreme — confirms overextension, not just noise
  3. ATR-based stop/target — sized to actual volatility, not a fixed %,
     so a stop in a calm stock isn't the same distance as in a wild one
  4. Deceleration check — only reversion-trade when the extreme move is
     LOSING steam (bar-over-bar range shrinking), not accelerating —
     avoids catching a falling knife mid-crash

Confidence scoring mirrors technical_agent.py's structure so MetaAgent
treats it consistently, but the entry logic is orthogonal to every other
directional agent in the roster.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import yfinance as yf

log = logging.getLogger("VolatilityAgent")

# Liquid names with reliable mean-reversion behavior — avoid low-float/illiquid names
WATCHLIST = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
]

BB_PERIOD        = 20
BB_STD           = 2.0
RSI_PERIOD       = 14
RSI_EXTREME_LOW  = 25
RSI_EXTREME_HIGH = 75
ATR_PERIOD       = 14
ATR_STOP_MULT    = 1.5   # stop = 1.5x ATR from entry
ATR_TARGET_MULT  = 2.5   # target = 2.5x ATR — ~1.7:1 reward/risk
MIN_CONFIDENCE   = 0.55

DEFAULT_EXPIRY_DAYS = 14


class VolatilityAgent:
    """Mean-reversion on statistical overextension. Orthogonal to directional agents."""

    name = "VolatilityAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST

    def generate_signals(self) -> list[dict]:
        signals = []
        for symbol in self.watchlist:
            try:
                signal = self._analyze(symbol)
                if signal:
                    signals.append(signal)
            except Exception as e:
                log.warning(f"VolatilityAgent: error analyzing {symbol}: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        df = self._fetch(symbol)
        if df is None or len(df) < BB_PERIOD + ATR_PERIOD + 5:
            return None

        ind = self._compute_indicators(df)
        direction, confidence, reason = self._evaluate(ind)
        if direction is None or confidence < MIN_CONFIDENCE:
            return None

        price = ind["price"]
        atr   = ind["atr"]

        if direction == "long":
            stop   = round(price - atr * ATR_STOP_MULT, 2)
            target = round(price + atr * ATR_TARGET_MULT, 2)
        else:
            stop   = round(price + atr * ATR_STOP_MULT, 2)
            target = round(price - atr * ATR_TARGET_MULT, 2)

        return {
            "agent":            self.name,
            "strategy":         "single_leg_calls" if direction == "long" else "single_leg_puts",
            "instrument_type":  "options",
            "symbol":           symbol,
            "direction":        direction,
            "entry_price":      round(price, 2),
            "stop_loss_price":  stop,
            "target_price":     target,
            "option_premium":   None,
            "futures_symbol":   None,
            "confidence":       confidence,
            "expiration":       self._next_expiry(DEFAULT_EXPIRY_DAYS),
            "meta_score":       confidence,
            "reasons":          [reason],
            "indicators":       ind,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _fetch(symbol: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True)
            if df is not None and len(df) >= 60:
                return df
            df = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=True)
            return None if df.empty else df
        except Exception as e:
            log.debug(f"yfinance fetch failed for {symbol}: {e}")
            return None

    @staticmethod
    def _compute_indicators(df: pd.DataFrame) -> dict:
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]

        # Bollinger %B — 0 = at lower band, 1 = at upper band, <0 or >1 = outside bands
        sma = close.rolling(BB_PERIOD).mean()
        std = close.rolling(BB_PERIOD).std()
        upper = sma + BB_STD * std
        lower = sma - BB_STD * std
        band_width = (upper - lower).iloc[-1]
        pct_b = float((close.iloc[-1] - lower.iloc[-1]) / band_width) if band_width else 0.5

        # RSI
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
        loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])

        # ATR (14) — true volatility measure for sizing
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(ATR_PERIOD).mean().iloc[-1])

        # Deceleration check — is the extension losing steam? Compare last
        # 3-bar range vs. prior 3-bar range. Shrinking range = losing momentum
        # = safer to fade. Expanding range = still accelerating = don't fight it.
        recent_range = float((high - low).iloc[-3:].mean())
        prior_range  = float((high - low).iloc[-6:-3].mean())
        decelerating = recent_range < prior_range * 0.95

        return {
            "price":        round(float(close.iloc[-1]), 2),
            "pct_b":        round(pct_b, 3),
            "rsi":          round(rsi, 2),
            "atr":          round(atr, 4),
            "decelerating": decelerating,
        }

    @staticmethod
    def _evaluate(ind: dict) -> tuple[Optional[str], float, str]:
        """Only fade extremes that are losing momentum — never fight an accelerating move."""
        if not ind["decelerating"]:
            return None, 0.0, ""

        # Overextended UP + overbought + decelerating → fade (short, expect reversion down)
        if ind["pct_b"] >= 1.05 and ind["rsi"] >= RSI_EXTREME_HIGH:
            extremity  = min((ind["pct_b"] - 1.0) * 2 + (ind["rsi"] - RSI_EXTREME_HIGH) / 25, 1.0)
            confidence = round(0.55 + extremity * 0.25, 3)
            return "short", confidence, (
                f"Overextended above upper BB (%B={ind['pct_b']:.2f}), "
                f"RSI overbought ({ind['rsi']:.1f}), momentum decelerating — mean reversion fade"
            )

        # Overextended DOWN + oversold + decelerating → fade (long, expect reversion up)
        if ind["pct_b"] <= -0.05 and ind["rsi"] <= RSI_EXTREME_LOW:
            extremity  = min(abs(ind["pct_b"]) * 2 + (RSI_EXTREME_LOW - ind["rsi"]) / 25, 1.0)
            confidence = round(0.55 + extremity * 0.25, 3)
            return "long", confidence, (
                f"Overextended below lower BB (%B={ind['pct_b']:.2f}), "
                f"RSI oversold ({ind['rsi']:.1f}), momentum decelerating — mean reversion bounce"
            )

        return None, 0.0, ""

    @staticmethod
    def _next_expiry(days_out: int) -> str:
        from datetime import timedelta
        today  = datetime.now(timezone.utc).date()
        target = today + timedelta(days=days_out)
        days_to_friday = (4 - target.weekday()) % 7
        return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent   = VolatilityAgent(watchlist=["SPY", "AAPL", "QQQ", "TSLA"])
    signals = agent.generate_signals()
    print(f"\nVolatilityAgent found {len(signals)} signal(s):\n")
    for s in signals:
        print(f"  {s['symbol']:6} {s['direction']:5} | conf: {s['confidence']} | {s['reasons']}")
