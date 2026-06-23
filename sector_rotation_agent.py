"""
sector_rotation_agent.py
------------------------
Identifies which market sectors are rotating into leadership and generates
signals on the leading sector ETFs and their top components.

Strategy:
  - Compute 1-month and 3-month relative performance of all 11 sector ETFs vs SPY
  - Identify top 2 leading sectors (outperforming SPY)
  - Identify bottom 2 lagging sectors (underperforming SPY)
  - Long leading sector ETFs + top component stocks
  - Short lagging sector ETFs when macro/sentiment are weak

Sector ETFs:
  XLK (Tech), XLC (Comms), XLY (Discretionary), XLP (Staples),
  XLV (Healthcare), XLF (Financials), XLI (Industrials),
  XLB (Materials), XLE (Energy), XLU (Utilities), XLRE (Real Estate)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("SectorRotationAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

SECTORS = {
    "XLK":  {"name": "Technology",          "top_stocks": ["MSFT", "NVDA", "AAPL"]},
    "XLC":  {"name": "Communication",        "top_stocks": ["META", "GOOGL", "NFLX"]},
    "XLY":  {"name": "Discretionary",        "top_stocks": ["AMZN", "TSLA", "HD"]},
    "XLP":  {"name": "Staples",              "top_stocks": ["PG", "KO", "WMT"]},
    "XLV":  {"name": "Healthcare",           "top_stocks": ["LLY", "UNH", "JNJ"]},
    "XLF":  {"name": "Financials",           "top_stocks": ["JPM", "BAC", "GS"]},
    "XLI":  {"name": "Industrials",          "top_stocks": ["CAT", "HON", "GE"]},
    "XLB":  {"name": "Materials",            "top_stocks": ["LIN", "APD", "NEM"]},
    "XLE":  {"name": "Energy",               "top_stocks": ["XOM", "CVX", "COP"]},
    "XLU":  {"name": "Utilities",            "top_stocks": ["NEE", "DUK", "SO"]},
    "XLRE": {"name": "Real Estate",          "top_stocks": ["PLD", "AMT", "CCI"]},
}

OUTPERFORM_THRESHOLD = 1.5   # sector must beat SPY by 1.5% over 1 month
UNDERPERFORM_THRESHOLD = -1.5
TOP_N_SECTORS    = 2
SHORT_N_SECTORS  = 1
STOP_LOSS_PCT    = 0.02
TARGET_PCT       = 0.05
MIN_CONFIDENCE   = 0.55
EXPIRY_DAYS      = 21


class SectorRotationAgent:
    name = "SectorRotationAgent"

    def __init__(self):
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "NEUTRAL"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            return []
        try:
            return self._analyze()
        except Exception as e:
            log.warning(f"SectorRotationAgent error: {e}")
            return []

    def _analyze(self) -> list[dict]:
        # Fetch SPY as benchmark
        spy = self._roc("SPY", days=21)
        if spy is None:
            return []

        # Score each sector
        sector_scores: list[tuple[str, float, float]] = []  # (etf, roc_1m, roc_3m)
        for etf in SECTORS:
            roc_1m = self._roc(etf, days=21)
            roc_3m = self._roc(etf, days=63)
            if roc_1m is None:
                continue
            sector_scores.append((etf, roc_1m, roc_3m or 0.0))

        if not sector_scores:
            return []

        # Relative performance vs SPY
        rel_scores = [
            (etf, r1m - spy, r3m)
            for etf, r1m, r3m in sector_scores
        ]
        rel_scores.sort(key=lambda x: x[1], reverse=True)

        signals = []

        # Long: top performing sectors beating SPY
        for etf, rel_1m, roc_3m in rel_scores[:TOP_N_SECTORS]:
            if rel_1m < OUTPERFORM_THRESHOLD:
                break
            price = self._price(etf)
            if price is None:
                continue
            factors = [
                f"{SECTORS[etf]['name']} ({etf}) outperforming SPY by +{rel_1m:.1f}% (1m)",
            ]
            if roc_3m > 0:
                factors.append(f"3-month ROC also positive (+{roc_3m:.1f}%)")

            # Check if a top component confirms
            for stock in SECTORS[etf]["top_stocks"][:2]:
                comp_roc = self._roc(stock, days=21)
                if comp_roc and comp_roc > 0:
                    factors.append(f"{stock} also rising (+{comp_roc:.1f}%)")
                    break

            confidence = 0.60 if len(factors) == 1 else (0.68 if len(factors) == 2 else 0.76)
            sig = self._build_signal(etf, price, "long", confidence, factors)
            if sig:
                signals.append(sig)

        # Short: worst performing sectors lagging SPY
        for etf, rel_1m, roc_3m in rel_scores[-SHORT_N_SECTORS:]:
            if rel_1m > UNDERPERFORM_THRESHOLD:
                continue
            price = self._price(etf)
            if price is None:
                continue
            factors = [
                f"{SECTORS[etf]['name']} ({etf}) underperforming SPY by {rel_1m:.1f}% (1m)",
            ]
            if roc_3m < 0:
                factors.append(f"3-month weakness too ({roc_3m:.1f}%)")

            confidence = 0.58 if len(factors) == 1 else 0.65
            sig = self._build_signal(etf, price, "short", confidence, factors)
            if sig:
                signals.append(sig)

        return signals

    def _build_signal(self, symbol: str, price: float, direction: str,
                      confidence: float, factors: list) -> Optional[dict]:
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
    def _roc(symbol: str, days: int) -> Optional[float]:
        try:
            df = yf.Ticker(symbol).history(period=f"{days + 10}d", interval="1d", auto_adjust=True)
            if df is None or len(df) < days:
                return None
            return float((df["Close"].iloc[-1] / df["Close"].iloc[-(days + 1)] - 1) * 100)
        except Exception:
            return None

    @staticmethod
    def _price(symbol: str) -> Optional[float]:
        try:
            df = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=True)
            return float(df["Close"].iloc[-1]) if df is not None and not df.empty else None
        except Exception:
            return None


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = SectorRotationAgent()
    sigs  = agent.generate_signals()
    print(f"SectorRotationAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
