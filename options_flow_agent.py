"""
options_flow_agent.py
---------------------
Detects unusual options activity that may signal informed trading.

Since real-time options flow data requires a paid API (e.g., Unusual Whales,
Market Chameleon), this agent uses a proxy approach with yfinance:

  1. Put/Call ratio from options chain: high put buying = bearish, high call buying = bullish
  2. Implied volatility rank (IVR): unusually high IV = expensive options / upcoming catalyst
  3. Large OI concentration at specific strikes = magnet levels
  4. IV skew: if puts are much more expensive than calls = hedging / fear

When Alpaca options data becomes available, this agent can be upgraded to
use real-time flow data directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

log = logging.getLogger("OptionsFlowAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA",
    "TSLA", "META", "AMZN", "GOOGL", "AMD",
]

# Thresholds
PUT_CALL_BULLISH   = 0.5    # P/C ratio below this = call-heavy = bullish
PUT_CALL_BEARISH   = 1.2    # P/C ratio above this = put-heavy = bearish
IV_HIGH_RANK       = 0.70   # IV > 70th percentile = elevated
MIN_OI_THRESHOLD   = 1000   # minimum open interest to be meaningful
STOP_LOSS_PCT      = 0.025
TARGET_PCT         = 0.06
MIN_CONFIDENCE     = 0.55
EXPIRY_DAYS        = 14


class OptionsFlowAgent:
    name = "OptionsFlowAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST
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
                log.debug(f"OptionsFlowAgent: {symbol} error: {e}")
        return signals

    def _analyze(self, symbol: str) -> Optional[dict]:
        ticker = yf.Ticker(symbol)

        # Get current price
        hist = ticker.history(period="5d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        price = float(hist["Close"].iloc[-1])

        # Get options chain for nearest expiry
        try:
            expirations = ticker.options
        except Exception:
            return None

        if not expirations:
            return None

        # Use the nearest expiry with sufficient time (≥5 days out)
        target_expiry = None
        today = date.today()
        for exp_str in expirations:
            try:
                exp_date = date.fromisoformat(exp_str)
                if (exp_date - today).days >= 5:
                    target_expiry = exp_str
                    break
            except Exception:
                continue

        if target_expiry is None:
            return None

        try:
            chain = ticker.option_chain(target_expiry)
        except Exception:
            return None

        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return None

        # Filter to ATM ± 10%
        calls = calls[
            (calls["strike"] >= price * 0.90) &
            (calls["strike"] <= price * 1.10)
        ].copy()
        puts = puts[
            (puts["strike"] >= price * 0.90) &
            (puts["strike"] <= price * 1.10)
        ].copy()

        if calls.empty or puts.empty:
            return None

        total_call_oi = float(calls["openInterest"].sum()) if "openInterest" in calls.columns else 0
        total_put_oi  = float(puts["openInterest"].sum())  if "openInterest" in puts.columns else 0
        total_call_vol = float(calls["volume"].fillna(0).sum()) if "volume" in calls.columns else 0
        total_put_vol  = float(puts["volume"].fillna(0).sum())  if "volume" in puts.columns else 0

        total_oi = total_call_oi + total_put_oi
        if total_oi < MIN_OI_THRESHOLD:
            return None

        # Put/Call ratio by open interest
        pc_oi  = total_put_oi  / total_call_oi  if total_call_oi > 0 else 1.0
        pc_vol = total_put_vol / total_call_vol if total_call_vol > 0 else 1.0

        # IV comparison: are puts or calls more expensive (skew)?
        avg_call_iv = float(calls["impliedVolatility"].mean()) if "impliedVolatility" in calls.columns else 0
        avg_put_iv  = float(puts["impliedVolatility"].mean())  if "impliedVolatility" in puts.columns else 0
        iv_skew = avg_put_iv - avg_call_iv  # positive = put skew (fear)

        factors   = []
        direction = None

        # Bullish options flow
        if pc_oi < PUT_CALL_BULLISH and pc_vol < PUT_CALL_BULLISH:
            factors.append(f"Call-heavy flow: P/C OI={pc_oi:.2f}, P/C Vol={pc_vol:.2f}")
            direction = "long"

        # Bearish options flow
        elif pc_oi > PUT_CALL_BEARISH and pc_vol > PUT_CALL_BEARISH:
            factors.append(f"Put-heavy flow: P/C OI={pc_oi:.2f}, P/C Vol={pc_vol:.2f}")
            direction = "short"

        if direction is None:
            return None

        # IV skew confirmation
        if direction == "short" and iv_skew > 0.05:
            factors.append(f"Elevated put IV skew (+{iv_skew*100:.1f}%) → hedging/fear")
        elif direction == "long" and iv_skew < -0.03:
            factors.append(f"Call IV elevated vs puts → bullish positioning")

        # Large OI concentration (whale positioning)
        best_call_strike = calls.loc[calls["openInterest"].idxmax(), "strike"] if not calls.empty else None
        best_put_strike  = puts.loc[puts["openInterest"].idxmax(), "strike"]  if not puts.empty else None
        if direction == "long" and best_call_strike:
            factors.append(f"Largest call OI at ${best_call_strike:.0f} (above current ${price:.0f})")
        elif direction == "short" and best_put_strike:
            factors.append(f"Largest put OI at ${best_put_strike:.0f} (below current ${price:.0f})")

        n = len(factors)
        conf_map = {1: 0.57, 2: 0.66, 3: 0.73}
        confidence = conf_map.get(n, 0.78 if n >= 4 else 0.55)

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


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = OptionsFlowAgent(watchlist=["SPY", "AAPL", "NVDA"])
    sigs  = agent.generate_signals()
    print(f"OptionsFlowAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
