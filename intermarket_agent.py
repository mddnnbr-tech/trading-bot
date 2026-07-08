"""
intermarket_agent.py
─────────────────────
Cross-asset driver → equity beneficiary mapping, on intraday data.

Fills the gap the ensemble had on 2026-07-08: WTI crude rallied all morning
and no agent connected it to CVX/XOM — MacroAgent only reads bonds/gold/
dollar on 10-day daily bars and only ever signals SPY/QQQ. This agent
watches the DRIVERS (futures, which trade nearly 24h and lead the equities)
and signals the direct BENEFICIARIES:

  WTI crude (CL=F)  up  → long CVX, XOM, XLE
  Gold     (GC=F)   up  → long NEM, GDX
  Copper   (HG=F)   up  → long FCX
  10Y yield (^TNX)  up  → long XLF   (banks earn more on spreads)
  WTI crude (CL=F) down → long DAL, UAL  (fuel is airlines' #2 cost)

Longs only, by design: the ensemble's shorts have been its consistent
loser (1W-3L in the clean epoch; MetaAgent now gates solo shorts anyway),
and every mapping above has a long-side expression of the driver's move
in either direction.

Timeframe: driver's move measured from today's session open on 5m bars —
i.e., "oil is up 1.5%+ TODAY", not a 10-day trend. That's the real-time
reaction window where the commodity→equity pass-through actually trades.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf

log = logging.getLogger("IntermarketAgent")

# driver symbol → (display name, threshold %, beneficiaries when UP, beneficiaries when DOWN)
DRIVER_MAP: dict[str, tuple[str, float, list[str], list[str]]] = {
    "CL=F":  ("WTI crude", 1.2, ["CVX", "XOM", "XLE"], ["DAL", "UAL"]),
    "GC=F":  ("Gold",      1.0, ["NEM", "GDX"],        []),
    "HG=F":  ("Copper",    1.5, ["FCX"],               []),
    "^TNX":  ("10Y yield", 2.5, ["XLF"],               []),
}

STOP_LOSS_PCT  = 0.02
TARGET_PCT     = 0.04   # tighter than trend agents — pass-through moves are same/next-day
MIN_CONFIDENCE = 0.55
EXPIRY_DAYS    = 7


class IntermarketAgent:
    """Signals equity beneficiaries of intraday commodity/rate moves."""

    name = "IntermarketAgent"

    def __init__(self):
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL", "NEUTRAL"]

    def generate_signals(self) -> list[dict]:
        signals = []
        for driver, (label, threshold, up_names, down_names) in DRIVER_MAP.items():
            try:
                move = self._session_move_pct(driver)
                if move is None:
                    continue

                if move >= threshold:
                    beneficiaries, phrase = up_names, f"{label} +{move:.1f}% today"
                elif move <= -threshold:
                    beneficiaries, phrase = down_names, f"{label} {move:.1f}% today"
                else:
                    continue

                # Confidence scales with how far past threshold the driver is
                excess     = abs(move) / threshold
                confidence = round(min(0.55 + (excess - 1.0) * 0.15, 0.80), 3)
                if confidence < MIN_CONFIDENCE:
                    continue

                for equity in beneficiaries:
                    sig = self._build_signal(equity, confidence, phrase)
                    if sig:
                        signals.append(sig)
            except Exception as e:
                log.warning(f"IntermarketAgent: error on driver {driver}: {e}")
        return signals

    @staticmethod
    def _session_move_pct(symbol: str) -> Optional[float]:
        """Driver's % move from today's session open to its latest 5m bar."""
        try:
            df = yf.Ticker(symbol).history(period="1d", interval="5m")
            if df is None or len(df) < 3:
                return None
            session_open = float(df["Open"].iloc[0])
            latest       = float(df["Close"].iloc[-1])
            if session_open <= 0:
                return None
            return (latest / session_open - 1) * 100
        except Exception:
            return None

    def _build_signal(self, symbol: str, confidence: float, driver_phrase: str) -> Optional[dict]:
        try:
            df = yf.Ticker(symbol).history(period="1d", interval="5m")
            if df is None or df.empty:
                return None
            price = float(df["Close"].iloc[-1])
        except Exception:
            return None
        if price <= 0:
            return None

        return {
            "agent":           self.name,
            "strategy":        "single_leg_calls",
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       "long",
            "entry_price":     round(price, 2),
            "stop_loss_price": round(price * (1 - STOP_LOSS_PCT), 2),
            "target_price":    round(price * (1 + TARGET_PCT), 2),
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      confidence,
            "expiration":      _next_friday(EXPIRY_DAYS),
            "meta_score":      confidence,
            "regime_affinity": self.regime_affinity,
            "reasons":         [f"{driver_phrase} → direct beneficiary"],
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = IntermarketAgent()
    sigs  = agent.generate_signals()
    print(f"IntermarketAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']:5} {s['direction']:5} conf={s['confidence']} | {s['reasons'][0]}")
