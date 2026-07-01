"""
crypto_agent.py
────────────────
Same technical-analysis logic as technical_agent.py, scoped to crypto and
running on its own 24/7 schedule (see crypto_scheduler.py) instead of being
gated by equity market hours.

Fixes a latent bug: technical_agent.py's WATCHLIST already listed
"BTC/USD", "ETH/USD", "SOL/USD" — but yfinance requires the "BTC-USD"
dash format, not the "BTC/USD" slash format Alpaca uses for order
submission. Every crypto fetch inside the equity-hours loop was silently
failing (caught by the try/except, logged as a debug warning, never
surfaced) — so crypto signal generation has likely never actually worked
despite the infrastructure (order_executor's CRYPTO_SYMBOLS handling,
watchlist entries) being in place for it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import yfinance as yf

log = logging.getLogger("CryptoAgent")

# Alpaca order format (slash) → yfinance fetch format (dash)
CRYPTO_SYMBOLS = {
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
}

RSI_PERIOD      = 14
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
BB_PERIOD       = 20
BB_STD          = 2.0

STOP_LOSS_PCT   = 0.03   # crypto is more volatile than equities — wider stop
TARGET_PCT      = 0.06
MIN_CONFIDENCE  = 0.55


class CryptoAgent:
    """Technical signals for BTC/ETH/SOL, fetchable any time (crypto never closes)."""

    name = "CryptoAgent"

    def generate_signals(self) -> list[dict]:
        signals = []
        for alpaca_symbol, yf_symbol in CRYPTO_SYMBOLS.items():
            try:
                signal = self._analyze(alpaca_symbol, yf_symbol)
                if signal:
                    signals.append(signal)
            except Exception as e:
                log.warning(f"CryptoAgent: error analyzing {alpaca_symbol}: {e}")
        return signals

    def _analyze(self, alpaca_symbol: str, yf_symbol: str) -> Optional[dict]:
        df = self._fetch(yf_symbol)
        if df is None or len(df) < MACD_SLOW + MACD_SIGNAL + 5:
            return None

        ind = self._compute_indicators(df)
        direction, confidence, reasons = self._evaluate(ind)
        if direction is None or confidence < MIN_CONFIDENCE:
            return None

        price = float(df["Close"].iloc[-1])
        stop   = round(price * (1 - STOP_LOSS_PCT) if direction == "long" else price * (1 + STOP_LOSS_PCT), 2)
        target = round(price * (1 + TARGET_PCT) if direction == "long" else price * (1 - TARGET_PCT), 2)

        return {
            "agent":            self.name,
            "strategy":         "crypto_spot",
            "instrument_type":  "crypto",
            "symbol":           alpaca_symbol,
            "direction":        direction,
            "entry_price":      round(price, 2),
            "stop_loss_price":  stop,
            "target_price":     target,
            "option_premium":   None,
            "futures_symbol":   None,
            "confidence":       confidence,
            "expiration":       None,
            "meta_score":       confidence,
            "reasons":          reasons,
            "indicators":       ind,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _fetch(yf_symbol: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.Ticker(yf_symbol).history(period="5d", interval="1h", auto_adjust=True)
            return df if (df is not None and len(df) >= 60) else None
        except Exception as e:
            log.debug(f"yfinance fetch failed for {yf_symbol}: {e}")
            return None

    @staticmethod
    def _compute_indicators(df: pd.DataFrame) -> dict:
        close = df["Close"]

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
        loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])

        ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
        ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
        macd_cross_up = (float(macd_line.iloc[-1]) > float(signal_line.iloc[-1]) and
                         float(macd_line.iloc[-2]) <= float(signal_line.iloc[-2]))
        macd_cross_dn = (float(macd_line.iloc[-1]) < float(signal_line.iloc[-1]) and
                         float(macd_line.iloc[-2]) >= float(signal_line.iloc[-2]))

        sma = close.rolling(BB_PERIOD).mean()
        std = close.rolling(BB_PERIOD).std()
        upper_bb = float((sma + BB_STD * std).iloc[-1])
        lower_bb = float((sma - BB_STD * std).iloc[-1])
        price_now = float(close.iloc[-1])

        return {
            "price": round(price_now, 2), "rsi": round(rsi, 2),
            "macd_cross_up": macd_cross_up, "macd_cross_dn": macd_cross_dn,
            "upper_bb": round(upper_bb, 2), "lower_bb": round(lower_bb, 2),
        }

    @staticmethod
    def _evaluate(ind: dict) -> tuple[Optional[str], float, list[str]]:
        bull, bear = [], []

        if ind["rsi"] < RSI_OVERSOLD:
            bull.append(f"RSI oversold ({ind['rsi']:.1f})")
        elif ind["rsi"] > RSI_OVERBOUGHT:
            bear.append(f"RSI overbought ({ind['rsi']:.1f})")

        if ind["macd_cross_up"]:
            bull.append("MACD bullish crossover")
        elif ind["macd_cross_dn"]:
            bear.append("MACD bearish crossover")

        if ind["price"] <= ind["lower_bb"]:
            bull.append(f"Price at lower BB (${ind['price']} <= ${ind['lower_bb']})")
        elif ind["price"] >= ind["upper_bb"]:
            bear.append(f"Price at upper BB (${ind['price']} >= ${ind['upper_bb']})")

        if len(bull) > len(bear) and bull:
            count, reasons, direction = len(bull), bull, "long"
        elif len(bear) > len(bull) and bear:
            count, reasons, direction = len(bear), bear, "short"
        else:
            return None, 0.0, []

        confidence_map = {1: 0.55, 2: 0.68, 3: 0.80}
        return direction, confidence_map.get(count, 0.85), reasons


if __name__ == "__main__":
    agent = CryptoAgent()
    signals = agent.generate_signals()
    print(f"\nCryptoAgent found {len(signals)} signal(s):\n")
    for s in signals:
        print(f"  {s['symbol']:8} {s['direction']:5} | conf: {s['confidence']} | {s['reasons']}")
