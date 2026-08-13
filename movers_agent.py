"""
movers_agent.py
────────────────
Dynamic-universe momentum continuation — the whole market, not a watchlist.

Fills the structural blindness found 2026-07-20: every other agent scans a
hardcoded list of ~40 established symbols, so a new listing (SPCX down 45%
since IPO) or any name outside the list could crash or moon all day and the
system would never look at it. This agent pulls Yahoo's live day_gainers /
day_losers screens each tick — whatever is moving hardest RIGHT NOW is the
universe.

Strategy: intraday momentum continuation, the classic day-trading edge —
stocks making outsized moves on real volume tend to extend intraday
(institutions can't finish repositioning in an hour). Gainers → long
continuation. Losers → short continuation. Confidence scales with the
size of the move; trailing stops handle the exit either way.

Filters: |move| >= 5%, price >= $5 (no penny-stock garbage), volume >=
500k (no illiquid traps).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import yfinance as yf

log = logging.getLogger("MoversAgent")

MIN_MOVE_PCT   = 5.0
MIN_PRICE      = 5.0
MIN_VOLUME     = 500_000
MAX_PER_SIDE   = 5        # top N gainers + top N losers considered per tick
STOP_LOSS_PCT  = 0.04     # movers are volatile — wider stop; trail% derives from this
TARGET_PCT     = 0.12     # bookkeeping marker only; real exit is the trailing stop


class MoversAgent:
    """Scans live market-wide biggest movers; trades continuation."""

    name = "MoversAgent"

    def __init__(self):
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL"]

    def generate_signals(self) -> list[dict]:
        signals = []
        for screen, direction in (("day_gainers", "long"), ("day_losers", "short")):
            try:
                res = yf.screen(screen)
                quotes = (res.get("quotes") or [])[:25]
            except Exception as e:
                log.debug(f"MoversAgent: screen {screen} failed: {e}")
                continue

            taken = 0
            for q in quotes:
                if taken >= MAX_PER_SIDE:
                    break
                try:
                    sym   = q.get("symbol", "")
                    pct   = float(q.get("regularMarketChangePercent") or 0)
                    price = float(q.get("regularMarketPrice") or 0)
                    vol   = float(q.get("regularMarketVolume") or 0)
                except (TypeError, ValueError):
                    continue

                if (not sym or "." in sym or "-" in sym
                        or abs(pct) < MIN_MOVE_PCT
                        or price < MIN_PRICE
                        or vol < MIN_VOLUME):
                    continue

                # Confidence must clear the gates this signal has to pass,
                # or the agent is decorative. MetaAgent requires 0.65 for a
                # solo long and 0.72 for a solo short. The old curve was
                # 0.60 + 0.02/pt capped at 0.80, which meant:
                #     a  5% gainer scored 0.60 -> under 0.65, never bought
                #     a 10% loser  scored 0.70 -> under 0.72, never shorted
                # A stock down 10% on the day — exactly the move we most
                # want to short — could not clear the bar by itself, and
                # this agent exists precisely to act alone on names no
                # other agent is watching.
                #
                # New curve: 0.66 at 5%, +0.03 per extra point, cap 0.88.
                #     5%  -> 0.66  clears solo long
                #     7%  -> 0.72  clears solo short
                #    10%  -> 0.81  strong short
                #    12%+ -> 0.87+ high conviction
                # Risk of chasing is handled where it belongs: the falling-
                # knife guard screens longs, and ATR stops (now uncapped to
                # 12%) size these positions smaller because they are volatile.
                confidence = round(min(0.66 + (abs(pct) - MIN_MOVE_PCT) * 0.03, 0.88), 3)

                if direction == "long":
                    stop   = round(price * (1 - STOP_LOSS_PCT), 2)
                    target = round(price * (1 + TARGET_PCT), 2)
                    strat  = "single_leg_calls"
                else:
                    stop   = round(price * (1 + STOP_LOSS_PCT), 2)
                    target = round(price * (1 - TARGET_PCT), 2)
                    strat  = "single_leg_puts"

                signals.append({
                    "agent":           self.name,
                    "strategy":        strat,
                    "instrument_type": "options",
                    "symbol":          sym,
                    "direction":       direction,
                    "entry_price":     round(price, 2),
                    "stop_loss_price": stop,
                    "target_price":    target,
                    "option_premium":  None,
                    "futures_symbol":  None,
                    "confidence":      confidence,
                    "expiration":      _next_friday(7),
                    "meta_score":      confidence,
                    "regime_affinity": self.regime_affinity,
                    "reasons":         [f"Market-wide mover: {pct:+.1f}% today on "
                                        f"{vol:,.0f} volume — continuation "
                                        f"{'long' if direction == 'long' else 'short'}"],
                    "timestamp":       datetime.now(timezone.utc).isoformat(),
                })
                taken += 1

        if signals:
            log.info(f"MoversAgent: {len(signals)} signal(s) from live movers screens")
        return signals


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    return (target + timedelta(days=(4 - target.weekday()) % 7)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = MoversAgent()
    for s in agent.generate_signals():
        print(f"{s['symbol']:8} {s['direction']:5} conf={s['confidence']} | {s['reasons'][0]}")
