"""
sentiment_agent.py
──────────────────
Reads market-wide sentiment indicators and generates directional bias signals.

Data sources (all free):
  • VIX level (via yfinance ^VIX)
  • Put/Call ratio (via yfinance ^CPC or CBOE feed)
  • CNN Fear & Greed Index (public API)
  • SPY 5-day momentum (proxy for market trend)

Signal logic:
  • Extreme fear (F&G < 20) + VIX spike → contrarian BUY bias
  • Extreme greed (F&G > 80) + low VIX → caution / reduced confidence
  • High put/call ratio → potential reversal up
  • Generates a market-wide sentiment signal for MetaAgent to weight
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

log = logging.getLogger("SentimentAgent")

# ── Thresholds ─────────────────────────────────────────────────────────────
VIX_HIGH        = 25.0   # elevated fear above this
VIX_EXTREME     = 35.0   # extreme fear (from .env VIX_HALT_THRESHOLD = 35)
FEAR_GREED_FEAR = 25     # extreme fear below this
FEAR_GREED_GREED = 75    # extreme greed above this
PUT_CALL_HIGH   = 1.20   # bearish sentiment above this
PUT_CALL_LOW    = 0.70   # bullish sentiment below this

STOP_LOSS_PCT   = 0.025
TARGET_PCT      = 0.04
MIN_CONFIDENCE  = 0.52

# Symbols for sentiment proxies
VIX_SYMBOL      = "^VIX"
SPY_SYMBOL      = "SPY"


class SentimentAgent:
    """Reads macro sentiment indicators and returns a market-wide signal."""

    name = "SentimentAgent"

    def generate_signals(self) -> list[dict]:
        """Return 0 or 1 market-wide sentiment signal."""
        try:
            sentiment = self._read_sentiment()
            signal    = self._evaluate(sentiment)
            return [signal] if signal else []
        except Exception as e:
            log.warning(f"SentimentAgent error: {e}")
            return []

    # ── Data collection ────────────────────────────────────────────────────

    def _read_sentiment(self) -> dict:
        vix        = self._get_vix()
        fear_greed = self._get_fear_greed()
        put_call   = self._get_put_call_ratio()
        spy_mom    = self._get_spy_momentum()

        log.info(
            f"Sentiment: VIX={vix:.1f}  F&G={fear_greed}  "
            f"P/C={put_call:.2f}  SPY_mom={spy_mom:+.2f}%"
        )
        return {
            "vix":        vix,
            "fear_greed": fear_greed,
            "put_call":   put_call,
            "spy_mom":    spy_mom,
        }

    @staticmethod
    def _get_vix() -> float:
        try:
            df = yf.Ticker(VIX_SYMBOL).history(period="2d")
            return float(df["Close"].iloc[-1]) if not df.empty else 20.0
        except Exception:
            return 20.0

    @staticmethod
    def _get_fear_greed() -> int:
        """Fetch CNN Fear & Greed index (0=extreme fear, 100=extreme greed)."""
        try:
            url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                score = data["fear_and_greed"]["score"]
                return int(float(score))
        except Exception:
            return 50   # neutral fallback

    @staticmethod
    def _get_put_call_ratio() -> float:
        """
        CBOE equity put/call ratio via yfinance ^CPCE.
        Falls back to 1.0 (neutral) if unavailable.
        """
        import logging as _logging
        import warnings
        # Suppress yfinance noise for this lookup
        for symbol in ("^CPCE", "^PCALL"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    _logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
                    df = yf.Ticker(symbol).history(period="2d")
                    _logging.getLogger("yfinance").setLevel(_logging.WARNING)
                    if not df.empty:
                        return float(df["Close"].iloc[-1])
            except Exception:
                continue
        return 1.0   # neutral fallback — no data available

    @staticmethod
    def _get_spy_momentum() -> float:
        """5-day price change % for SPY as a broad trend proxy."""
        try:
            df = yf.Ticker(SPY_SYMBOL).history(period="10d")
            if len(df) >= 5:
                return float((df["Close"].iloc[-1] / df["Close"].iloc[-5] - 1) * 100)
        except Exception:
            pass
        return 0.0

    # ── Signal evaluation ──────────────────────────────────────────────────

    def _evaluate(self, s: dict) -> Optional[dict]:
        bull_score = 0
        bear_score = 0
        reasons    = []

        # VIX signals
        if s["vix"] >= VIX_EXTREME:
            # At halt threshold — no signal (risk_agent will veto anyway)
            log.warning(f"VIX at {s['vix']:.1f} — near halt threshold, skipping sentiment signal")
            return None
        elif s["vix"] >= VIX_HIGH:
            # Elevated fear — slight contrarian bull bias
            bull_score += 1
            reasons.append(f"VIX elevated ({s['vix']:.1f}) — contrarian bull lean")
        elif s["vix"] < 15:
            # Very low VIX — complacency, slight bear lean
            bear_score += 1
            reasons.append(f"VIX very low ({s['vix']:.1f}) — complacency warning")

        # Fear & Greed
        if s["fear_greed"] <= FEAR_GREED_FEAR:
            bull_score += 2
            reasons.append(f"Extreme fear (F&G={s['fear_greed']}) — contrarian BUY signal")
        elif s["fear_greed"] >= FEAR_GREED_GREED:
            bear_score += 1
            reasons.append(f"Extreme greed (F&G={s['fear_greed']}) — caution")

        # Put/Call ratio
        if s["put_call"] >= PUT_CALL_HIGH:
            bull_score += 1
            reasons.append(f"High put/call ratio ({s['put_call']:.2f}) — bearish positioning, potential reversal")
        elif s["put_call"] <= PUT_CALL_LOW:
            bear_score += 1
            reasons.append(f"Low put/call ratio ({s['put_call']:.2f}) — bullish complacency")

        # SPY momentum
        if s["spy_mom"] > 1.5:
            bull_score += 1
            reasons.append(f"SPY 5-day momentum strong ({s['spy_mom']:+.1f}%)")
        elif s["spy_mom"] < -1.5:
            bear_score += 1
            reasons.append(f"SPY 5-day momentum weak ({s['spy_mom']:+.1f}%)")

        total = bull_score + bear_score
        if total == 0:
            return None   # neutral — no signal

        if bull_score > bear_score:
            direction  = "long"
            confidence = min(0.50 + (bull_score / total) * 0.35, 0.75)
        elif bear_score > bull_score:
            direction  = "short"
            confidence = min(0.50 + (bear_score / total) * 0.35, 0.75)
        else:
            return None

        if confidence < MIN_CONFIDENCE:
            return None

        # Use SPY as the sentiment signal instrument
        spy_price = self._get_spy_price()
        if spy_price is None:
            return None

        stop_loss = round(spy_price * (1 - STOP_LOSS_PCT) if direction == "long"
                          else spy_price * (1 + STOP_LOSS_PCT), 2)
        target    = round(spy_price * (1 + TARGET_PCT) if direction == "long"
                          else spy_price * (1 - TARGET_PCT), 2)

        from technical_agent import TechnicalAgent
        expiry = TechnicalAgent._next_expiry(21)  # slightly longer for sentiment plays

        return {
            "agent":           self.name,
            "strategy":        "single_leg_calls" if direction == "long" else "single_leg_puts",
            "instrument_type": "options",
            "symbol":          "SPY",
            "direction":       direction,
            "entry_price":     round(spy_price, 2),
            "stop_loss_price": stop_loss,
            "target_price":    target,
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      round(confidence, 3),
            "expiration":      expiry,
            "meta_score":      round(confidence, 3),
            "reasons":         reasons,
            "sentiment_data":  s,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _get_spy_price() -> Optional[float]:
        try:
            df = yf.Ticker(SPY_SYMBOL).history(period="1d")
            return float(df["Close"].iloc[-1]) if not df.empty else None
        except Exception:
            return None


# ── Quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    agent   = SentimentAgent()
    signals = agent.generate_signals()
    print(f"\nSentimentAgent found {len(signals)} signal(s):")
    for s in signals:
        print(f"  {s['symbol']} {s['direction']} | conf: {s['confidence']:.2f}")
        for r in s.get("reasons", []):
            print(f"    • {r}")
