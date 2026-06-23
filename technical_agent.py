"""
technical_agent.py
──────────────────
Generates trade signals from technical indicators.

Indicators used:
  • RSI (14-period)       — oversold/overbought
  • MACD (12/26/9)        — momentum crossover
  • Bollinger Bands (20)  — price vs. upper/lower band
  • SMA crossover (20/50) — trend direction

Confidence scoring:
  1 indicator aligned  → 0.50
  2 indicators aligned → 0.65
  3 indicators aligned → 0.75
  4 indicators aligned → 0.85

Data source: yfinance (free, no auth required for paper trading)
Install:  pip install yfinance --break-system-packages
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf
import pandas as pd

log = logging.getLogger("TechnicalAgent")

# ── Watchlist — symbols the agent scans each tick ─────────────────────────
# Edit this list to add/remove tickers
WATCHLIST = [
    # Major ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    # High-momentum / high-volume
    "PLTR", "COIN", "SOFI", "SMCI", "AVGO", "CRWD", "PANW", "SNOW",
    # Broad market movers
    "NFLX", "UBER", "ABNB", "SHOP", "SQ", "RBLX",
]

# ── Indicator parameters ──────────────────────────────────────────────────
RSI_PERIOD      = 14
RSI_OVERSOLD    = 35    # was 30 — wider zone means signals fire more often
RSI_OVERBOUGHT  = 65    # was 70 — catches overbought conditions earlier
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
BB_PERIOD       = 20
BB_STD          = 2.0
SMA_FAST        = 20
SMA_SLOW        = 50

# ── Options defaults (paper trading — adjust when live) ───────────────────
DEFAULT_EXPIRY_DAYS  = 14    # target weekly/monthly expirations
STOP_LOSS_PCT        = 0.02  # 2% stop loss below entry
TARGET_PCT           = 0.05  # 5% profit target (was 4%) — better risk/reward
MIN_CONFIDENCE       = 0.55  # aligned with AgentRiskBridge threshold — prevents single-indicator signals from being generated only to be rejected


class TechnicalAgent:
    """Scans the watchlist and emits signals based on technical analysis."""

    name = "TechnicalAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST

    def generate_signals(self) -> list[dict]:
        """
        Scan all watchlist symbols and return a list of raw signals.
        Each signal is ready to pass into AgentRiskBridge.evaluate_signal().
        """
        signals = []
        for symbol in self.watchlist:
            try:
                signal = self._analyze(symbol)
                if signal:
                    signals.append(signal)
            except Exception as e:
                log.warning(f"TechnicalAgent: error analyzing {symbol}: {e}")
        return signals

    # ── Core analysis ──────────────────────────────────────────────────────

    def _analyze(self, symbol: str) -> Optional[dict]:
        """Run all indicators on a symbol. Return signal dict or None."""
        df = self._fetch(symbol)
        if df is None or len(df) < MACD_SLOW + MACD_SIGNAL + 5:
            return None

        indicators = self._compute_indicators(df)
        direction, confidence, reasons = self._evaluate(indicators)

        if direction is None or confidence < MIN_CONFIDENCE:
            return None

        price     = float(df["Close"].iloc[-1])
        stop_loss = round(price * (1 - STOP_LOSS_PCT) if direction == "long"
                          else price * (1 + STOP_LOSS_PCT), 2)
        target    = round(price * (1 + TARGET_PCT) if direction == "long"
                          else price * (1 - TARGET_PCT), 2)

        expiry = self._next_expiry(DEFAULT_EXPIRY_DAYS)

        return {
            "agent":            self.name,
            "strategy":         "single_leg_calls" if direction == "long" else "single_leg_puts",
            "instrument_type":  "options",
            "symbol":           symbol,
            "direction":        direction,
            "entry_price":      round(price, 2),
            "stop_loss_price":  stop_loss,
            "target_price":     target,
            "option_premium":   None,   # filled by order execution module
            "futures_symbol":   None,
            "confidence":       confidence,
            "expiration":       expiry,
            "meta_score":       confidence,
            "reasons":          reasons,
            "indicators":       indicators,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        }

    # ── Indicators ─────────────────────────────────────────────────────────

    @staticmethod
    def _fetch(symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch 5-minute intraday bars for the last 5 days.
        Falls back to daily if intraday fetch fails.
        5-minute data gives current-session signals instead of yesterday's close.
        """
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="5d", interval="5m", auto_adjust=True)
            if df is not None and len(df) >= 60:
                return df
            # Fallback: daily data if intraday unavailable
            df = ticker.history(period="6mo", interval="1d", auto_adjust=True)
            if df.empty:
                return None
            return df
        except Exception as e:
            log.debug(f"yfinance fetch failed for {symbol}: {e}")
            return None

    @staticmethod
    def _compute_indicators(df: pd.DataFrame) -> dict:
        close = df["Close"]

        # RSI
        delta   = close.diff()
        gain    = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
        loss    = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
        rs      = gain / loss.replace(0, float("nan"))
        rsi     = float((100 - 100 / (1 + rs)).iloc[-1])

        # MACD
        ema_fast   = close.ewm(span=MACD_FAST,   adjust=False).mean()
        ema_slow   = close.ewm(span=MACD_SLOW,   adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
        macd_hist  = float((macd_line - signal_line).iloc[-1])
        macd_cross = (float(macd_line.iloc[-1]) > float(signal_line.iloc[-1]) and
                      float(macd_line.iloc[-2]) <= float(signal_line.iloc[-2]))
        macd_cross_down = (float(macd_line.iloc[-1]) < float(signal_line.iloc[-1]) and
                           float(macd_line.iloc[-2]) >= float(signal_line.iloc[-2]))

        # Bollinger Bands
        sma_bb    = close.rolling(BB_PERIOD).mean()
        std_bb    = close.rolling(BB_PERIOD).std()
        upper_bb  = float((sma_bb + BB_STD * std_bb).iloc[-1])
        lower_bb  = float((sma_bb - BB_STD * std_bb).iloc[-1])
        price_now = float(close.iloc[-1])

        # SMA crossover
        sma_fast_val  = float(close.rolling(SMA_FAST).mean().iloc[-1])
        sma_slow_val  = float(close.rolling(SMA_SLOW).mean().iloc[-1])
        sma_fast_prev = float(close.rolling(SMA_FAST).mean().iloc[-2])
        sma_slow_prev = float(close.rolling(SMA_SLOW).mean().iloc[-2])
        golden_cross  = sma_fast_val > sma_slow_val and sma_fast_prev <= sma_slow_prev
        death_cross   = sma_fast_val < sma_slow_val and sma_fast_prev >= sma_slow_prev

        # RSI momentum — compare current vs 3-day-ago RSI
        rsi_series  = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
        rsi_prev3   = float(rsi_series.iloc[-4]) if len(rsi_series) >= 4 else rsi
        rsi_rising  = rsi > rsi_prev3 + 2
        rsi_falling = rsi < rsi_prev3 - 2

        return {
            "rsi":           round(rsi, 2),
            "rsi_rising":    rsi_rising,
            "rsi_falling":   rsi_falling,
            "macd_hist":     round(macd_hist, 4),
            "macd_cross_up": macd_cross,
            "macd_cross_dn": macd_cross_down,
            "price":         round(price_now, 2),
            "upper_bb":      round(upper_bb, 2),
            "lower_bb":      round(lower_bb, 2),
            "sma_fast":      round(sma_fast_val, 2),
            "sma_slow":      round(sma_slow_val, 2),
            "golden_cross":  golden_cross,
            "death_cross":   death_cross,
        }

    @staticmethod
    def _evaluate(ind: dict) -> tuple[Optional[str], float, list[str]]:
        """Score indicators and decide direction + confidence."""
        bull_signals = []
        bear_signals = []

        # RSI — oversold/overbought
        if ind["rsi"] < RSI_OVERSOLD:
            bull_signals.append(f"RSI oversold ({ind['rsi']:.1f})")
        elif ind["rsi"] > RSI_OVERBOUGHT:
            bear_signals.append(f"RSI overbought ({ind['rsi']:.1f})")

        # RSI momentum — trending strongly (catches mid-range moves)
        if 40 <= ind["rsi"] <= 55 and ind.get("rsi_rising"):
            bull_signals.append(f"RSI bullish momentum ({ind['rsi']:.1f} rising)")
        elif 45 <= ind["rsi"] <= 60 and ind.get("rsi_falling"):
            bear_signals.append(f"RSI bearish momentum ({ind['rsi']:.1f} falling)")

        # MACD crossover
        if ind["macd_cross_up"]:
            bull_signals.append("MACD bullish crossover")
        elif ind["macd_cross_dn"]:
            bear_signals.append("MACD bearish crossover")

        # MACD histogram direction (catches momentum before crossover)
        if ind["macd_hist"] > 0.10:
            bull_signals.append(f"MACD histogram positive ({ind['macd_hist']:.3f})")
        elif ind["macd_hist"] < -0.10:
            bear_signals.append(f"MACD histogram negative ({ind['macd_hist']:.3f})")

        # Bollinger Bands
        if ind["price"] <= ind["lower_bb"]:
            bull_signals.append(f"Price at lower BB (${ind['price']} ≤ ${ind['lower_bb']})")
        elif ind["price"] >= ind["upper_bb"]:
            bear_signals.append(f"Price at upper BB (${ind['price']} ≥ ${ind['upper_bb']})")

        # SMA crossover
        if ind["golden_cross"]:
            bull_signals.append("Golden cross (SMA20 crossed above SMA50)")
        elif ind["death_cross"]:
            bear_signals.append("Death cross (SMA20 crossed below SMA50)")

        # SMA trend (price above/below SMA50 as directional bias)
        if ind["price"] > ind["sma_slow"] * 1.02:
            bull_signals.append(f"Price above SMA50 (+{((ind['price']/ind['sma_slow'])-1)*100:.1f}%)")
        elif ind["price"] < ind["sma_slow"] * 0.98:
            bear_signals.append(f"Price below SMA50 ({((ind['price']/ind['sma_slow'])-1)*100:.1f}%)")

        # Determine direction — require strict majority (not just more than other side)
        if len(bull_signals) > len(bear_signals) and len(bull_signals) >= 1:
            direction = "long"
            count     = len(bull_signals)
            reasons   = bull_signals
        elif len(bear_signals) > len(bull_signals) and len(bear_signals) >= 1:
            direction = "short"
            count     = len(bear_signals)
            reasons   = bear_signals
        else:
            return None, 0.0, []   # no consensus

        # Confidence from indicator count — more indicators = more conviction
        confidence_map = {1: 0.48, 2: 0.62, 3: 0.73, 4: 0.82}
        confidence = confidence_map.get(count, 0.88 if count >= 5 else 0.48)

        return direction, confidence, reasons

    @staticmethod
    def _next_expiry(days_out: int) -> str:
        """Return the nearest Friday at least 'days_out' calendar days away."""
        from datetime import timedelta
        today   = datetime.now(timezone.utc).date()
        target  = today + timedelta(days=days_out)
        # roll forward to Friday (weekday 4)
        days_to_friday = (4 - target.weekday()) % 7
        expiry  = target + timedelta(days=days_to_friday)
        return expiry.strftime("%Y-%m-%d")


# ── Quick test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    agent   = TechnicalAgent(watchlist=["SPY", "AAPL", "QQQ"])
    signals = agent.generate_signals()
    print(f"\nTechnicalAgent found {len(signals)} signal(s):\n")
    for s in signals:
        print(f"  {s['symbol']:6} {s['direction']:5} | conf: {s['confidence']} | reasons: {s['reasons']}")
