"""
news_agent.py
─────────────
Generates trade signals from financial news RSS feeds.

Sources (all free, no API key needed):
  • Yahoo Finance RSS (per ticker + market news)
  • MarketWatch RSS
  • Seeking Alpha RSS (public headlines)

Signal logic:
  • Scans headlines for strong positive/negative sentiment keywords
  • Weights recency (news > 2 hours old is discarded)
  • Only generates a signal when sentiment score exceeds threshold
  • Confidence scales with number of aligned headlines

Install:  pip install feedparser --break-system-packages
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import re

try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False
    logging.getLogger("NewsAgent").warning(
        "feedparser not installed. Run: pip install feedparser --break-system-packages"
    )

log = logging.getLogger("NewsAgent")

# ── Watchlist ──────────────────────────────────────────────────────────────
WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA",
    "TSLA", "AMZN", "META", "GOOGL", "AMD",
]

# ── News staleness threshold ───────────────────────────────────────────────
MAX_AGE_HOURS = 2   # ignore headlines older than this

# ── Keyword dictionaries ───────────────────────────────────────────────────
BULLISH_KEYWORDS = [
    "beats", "beat", "exceeds", "record", "upgrade", "upgraded", "buy",
    "outperform", "strong", "surge", "surges", "rally", "bullish",
    "breakout", "growth", "profit", "earnings beat", "raises guidance",
    "buyback", "acquisition", "partnership", "launch", "approved",
]
BEARISH_KEYWORDS = [
    "misses", "miss", "missed", "downgrade", "downgraded", "sell",
    "underperform", "weak", "drop", "drops", "plunge", "bearish",
    "breakdown", "loss", "recall", "investigation", "lawsuit", "fraud",
    "guidance cut", "layoffs", "bankruptcy", "default", "warning",
]

MIN_CONFIDENCE   = 0.55
STOP_LOSS_PCT    = 0.02
TARGET_PCT       = 0.035

# ── RSS feed templates ─────────────────────────────────────────────────────
def _feeds_for(symbol: str) -> list[str]:
    return [
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
        f"https://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
    ]

MARKET_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
]


class NewsAgent:
    """Reads RSS feeds and generates directional signals from news sentiment."""

    name = "NewsAgent"

    def __init__(self, watchlist: list[str] | None = None):
        self.watchlist = watchlist or WATCHLIST

    def generate_signals(self) -> list[dict]:
        """Return a list of raw signals ready for AgentRiskBridge."""
        if not _FEEDPARSER_AVAILABLE:
            log.error("feedparser not installed — NewsAgent cannot run.")
            return []

        signals = []
        for symbol in self.watchlist:
            try:
                signal = self._analyze(symbol)
                if signal:
                    signals.append(signal)
            except Exception as e:
                log.warning(f"NewsAgent: error analyzing {symbol}: {e}")
        return signals

    # ── Core analysis ──────────────────────────────────────────────────────

    def _analyze(self, symbol: str) -> Optional[dict]:
        headlines = self._fetch_headlines(symbol)
        if not headlines:
            return None

        bull_count = 0
        bear_count = 0
        matched_headlines = []

        for hl in headlines:
            text  = hl.lower()
            bulls = sum(1 for kw in BULLISH_KEYWORDS if kw in text)
            bears = sum(1 for kw in BEARISH_KEYWORDS if kw in text)
            if bulls > bears:
                bull_count += bulls
                matched_headlines.append(f"[BULL] {hl[:80]}")
            elif bears > bulls:
                bear_count += bears
                matched_headlines.append(f"[BEAR] {hl[:80]}")

        total = bull_count + bear_count
        if total == 0:
            return None

        if bull_count > bear_count:
            direction = "long"
            score     = bull_count / total
        elif bear_count > bull_count:
            direction = "short"
            score     = bear_count / total
        else:
            return None   # tied — no actionable signal

        # Confidence: scaled from score, capped
        confidence = min(0.50 + (score - 0.5) * 0.70, 0.80)
        if confidence < MIN_CONFIDENCE:
            return None

        # Get a rough price from yfinance for stop/target calculation
        price = self._get_price(symbol)
        if price is None:
            return None

        stop_loss = round(price * (1 - STOP_LOSS_PCT) if direction == "long"
                          else price * (1 + STOP_LOSS_PCT), 2)
        target    = round(price * (1 + TARGET_PCT) if direction == "long"
                          else price * (1 - TARGET_PCT), 2)

        from technical_agent import TechnicalAgent
        expiry = TechnicalAgent._next_expiry(14)

        return {
            "agent":           self.name,
            "strategy":        "single_leg_calls" if direction == "long" else "single_leg_puts",
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       direction,
            "entry_price":     round(price, 2),
            "stop_loss_price": stop_loss,
            "target_price":    target,
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      round(confidence, 3),
            "expiration":      expiry,
            "meta_score":      round(confidence, 3),
            "reasons":         matched_headlines[:5],
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fetch_headlines(self, symbol: str) -> list[str]:
        """Fetch recent headlines for a symbol from RSS feeds."""
        cutoff    = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
        headlines = []

        for url in _feeds_for(symbol):
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    published = self._parse_date(entry)
                    if published and published < cutoff:
                        continue
                    title = entry.get("title", "")
                    # Only include if headline mentions the symbol
                    if symbol.upper() in title.upper() or len(self.watchlist) == 1:
                        headlines.append(title)
            except Exception as e:
                log.debug(f"Feed fetch error ({url}): {e}")

        return headlines[:20]  # cap to 20 most recent

    @staticmethod
    def _parse_date(entry) -> Optional[datetime]:
        """Parse published date from feed entry."""
        try:
            import time
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            if t:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
        except Exception:
            pass
        return None

    @staticmethod
    def _get_price(symbol: str) -> Optional[float]:
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            hist   = ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return None


# ── Quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    agent   = NewsAgent(watchlist=["AAPL", "NVDA", "SPY"])
    signals = agent.generate_signals()
    print(f"\nNewsAgent found {len(signals)} signal(s):\n")
    for s in signals:
        print(f"  {s['symbol']:6} {s['direction']:5} | conf: {s['confidence']:.2f}")
        for r in s.get("reasons", [])[:3]:
            print(f"    {r}")
