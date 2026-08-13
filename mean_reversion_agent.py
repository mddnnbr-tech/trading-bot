"""
mean_reversion_agent.py
───────────────────────
Buys oversold pullbacks inside an established uptrend.

Why this agent exists. signal_research.py replayed sixteen candidate entry
rules over 81 symbols and 432,592 symbol-days (2005-2026, ~88k simulated
trades), every one run through production's own stop/trail/sizing so the
mechanics could not colour the comparison. The two best rules were both
buy-the-dip, and the breakout rule the ensemble leans on came near the
bottom of the long table:

    bb_reversion_long      +$92/trade   PF 1.68   profitable 19 of 22 years
    rsi_oversold_long      +$90/trade   PF 1.67   profitable 19 of 22 years
    trend_pullback_long    +$76/trade   PF 1.54   profitable 19 of 22 years
    ...
    breakout20_long        +$47/trade   PF 1.38   profitable 15 of 22 years

The ensemble had no mean-reversion voice at all — fifteen agents, all of
them momentum, breakout, news or macro. This adds the missing one.

Every rule here carries the same non-negotiable trend filter: price above
its 200-day average. These are pullbacks inside an uptrend, NOT falling
knives. Dropping that filter turns the same rules into catching downtrends,
which is how mean reversion earns its bad reputation.

IMPORTANT CAVEAT, because it bounds how much to trust the numbers above:
the research universe is symbols listed TODAY, so it carries survivorship
bias, and buy-the-dip is exactly the strategy that bias flatters most — a
dip in a company that survived 22 years always recovered. Expect live
results well below +$92. The RANKING against other long signals is the
durable finding (all candidates shared the bias); the absolute figure is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger("MeanReversionAgent")

MIN_PRICE   = 5.0
MIN_VOLUME  = 500_000
MAX_SIGNALS = 3          # per tick; these setups are common, keep them rare
RSI_FLOOR   = 20.0       # below this is usually news-driven, not noise

WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "AVGO", "NFLX",
    "CRM", "ORCL", "ADBE", "QCOM", "MU", "TXN", "CSCO", "IBM",
    "JPM", "GS", "BAC", "AXP", "MS", "SCHW",
    "XOM", "CVX", "COP", "SLB", "OXY",
    "UNH", "JNJ", "PFE", "MRK", "LLY", "ABT", "AMGN",
    "WMT", "COST", "HD", "LOW", "NKE", "SBUX", "MCD", "KO", "PG", "PEP",
    "DIS", "CMCSA", "CAT", "DE", "BA", "UPS",
    "SPY", "QQQ", "IWM", "XLE", "XLF", "XLK", "XLV",
]


def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


class MeanReversionAgent:
    name            = "MeanReversionAgent"
    regime_affinity = ["BULL", "NEUTRAL", "VOLATILE"]

    def __init__(self):
        self.watchlist = list(WATCHLIST)

    def generate_signals(self) -> list[dict]:
        signals: list[dict] = []
        try:
            data = yf.download(self.watchlist, period="1y", interval="1d",
                               group_by="ticker", progress=False,
                               auto_adjust=True, threads=True)
        except Exception as e:
            log.warning(f"MeanReversionAgent: download failed ({e})")
            return []

        cands = []
        for sym in self.watchlist:
            try:
                df = data[sym].dropna()
                if len(df) < 210:
                    continue
                c = df["Close"]
                px = float(c.iloc[-1])
                vol = float(df["Volume"].iloc[-1])
                if px < MIN_PRICE or vol < MIN_VOLUME:
                    continue

                sma20  = float(c.iloc[-20:].mean())
                sma200 = float(c.iloc[-200:].mean())
                # The trend filter. Without it these are falling knives.
                if px <= sma200:
                    continue

                r = float(_rsi(c).iloc[-1])
                sd = float(c.iloc[-20:].std())
                bb_lo = sma20 - 2 * sd
                if not np.isfinite(r) or not np.isfinite(bb_lo):
                    continue

                # Rank the two winning rules; band breach is the stronger.
                if px < bb_lo and r > RSI_FLOOR:
                    kind, base = "below lower Bollinger band", 0.72
                elif r < 30 and r > RSI_FLOOR:
                    kind, base = f"RSI {r:.0f} oversold", 0.70
                elif px < sma20 and 35 < r < 50:
                    kind, base = "pullback to trend", 0.66
                else:
                    continue

                # Deeper dip inside an intact uptrend = more conviction, but
                # cap it: past RSI_FLOOR a dip is usually news, not noise.
                depth = max(0.0, (sma20 - px) / sma20 * 100)
                conf = round(min(base + min(depth, 8) * 0.015, 0.86), 3)
                cands.append((conf, sym, px, kind, r, sma200))
            except Exception:
                continue

        cands.sort(reverse=True)
        for conf, sym, px, kind, r, sma200 in cands[:MAX_SIGNALS]:
            # Geometry is a placeholder — ensemble._normalize_geometry
            # replaces it with ATR-derived levels before execution.
            signals.append({
                "agent":           self.name,
                "strategy":        "mean_reversion",
                "instrument_type": "equity",
                "symbol":          sym,
                "direction":       "long",
                "entry_price":     round(px, 2),
                "stop_loss_price": round(px * 0.96, 2),
                "target_price":    round(px * 1.16, 2),
                "option_premium":  None,
                "futures_symbol":  None,
                "confidence":      conf,
                "expiration":      None,
                "meta_score":      conf,
                "regime_affinity": self.regime_affinity,
                "reasons": [
                    f"{kind} while {(px / sma200 - 1) * 100:+.1f}% above the "
                    f"200-day — oversold pullback inside an uptrend",
                    "backtest 2005-2026: PF 1.67-1.68, profitable in 19 of 22 years",
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        if signals:
            log.info(f"MeanReversionAgent: {len(signals)} pullback signal(s) — "
                     + ", ".join(s["symbol"] for s in signals))
        return signals
