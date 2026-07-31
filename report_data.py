"""
report_data.py
───────────────
ONE validated snapshot. Every report renders from this and nothing else.

Why this exists: daily_reporter.py grew to 1,806 lines reading 9 data
sources, 3 of them dead. Each section chose its own source, so three
separate bugs shipped in one week — all silent, all flattering:

  2026-07-30  ledger-sourced P&L claimed "+$2,376, beating SPY" during a
              week broker equity fell $19,908                 ($22k error)
  2026-07-31  "No approved trades today" while AXTI and CVX filled
              (section still read the dead trade_log.jsonl)
  2026-07-31  "+$4,209, equity $88,927" on a day the account was at
              $86,176 and down $2,750 — Alpaca's daily history bar only
              posts AFTER settlement, so eq[-1] is always yesterday
                                                            ($6,960 error)

The rule enforced here:
    BROKER is truth for money.  LEDGER is truth for attribution.
Nothing downstream gets to decide.

And critically: this SELF-VALIDATES. If ledger P&L and broker equity
disagree beyond tolerance, the snapshot carries a loud discrepancy that
the report prints at the top. A wrong number that announces itself is
recoverable; a wrong number that looks right is what cost this account
three days of bad decisions.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
ET = ZoneInfo("America/New_York")
log = logging.getLogger("ReportData")

PAPER_API = "https://paper-api.alpaca.markets"
START_EQUITY = 100_000.0
RECONCILE_TOLERANCE = 250.0     # $ gap that triggers a visible warning


def _hdr() -> dict:
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}


def snapshot() -> dict:
    """The single validated view. Never raises; degrades with warnings."""
    d: dict = {"warnings": [], "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
               "today": datetime.now(ET).strftime("%Y-%m-%d")}

    # ── MONEY: broker only ────────────────────────────────────────────
    try:
        a = requests.get(f"{PAPER_API}/v2/account", headers=_hdr(), timeout=15).json()
        d["equity"] = float(a.get("equity") or 0)
        d["prev_close"] = float(a.get("last_equity") or 0)
        d["buying_power"] = float(a.get("buying_power") or 0)
        d["options_bp"] = float(a.get("options_buying_power") or 0)
        d["day_pnl"] = d["equity"] - d["prev_close"]
        d["total_pnl"] = d["equity"] - START_EQUITY
        d["total_pct"] = (d["equity"] / START_EQUITY - 1) * 100
    except Exception as e:
        d["warnings"].append(f"CRITICAL: broker account unreachable ({e}) — "
                             f"all money figures below are unavailable, not zero")
        for k in ("equity", "prev_close", "buying_power", "options_bp",
                  "day_pnl", "total_pnl", "total_pct"):
            d[k] = None
        return d

    # Multi-day windows. eq[-1] is YESTERDAY (settled bars only) — the
    # live equity above is today, so windows compare live vs eq[-N].
    d["windows"] = []
    try:
        h = requests.get(f"{PAPER_API}/v2/account/portfolio/history",
                         params={"period": "3M", "timeframe": "1D"},
                         headers=_hdr(), timeout=15).json()
        eq = [e for e in (h.get("equity") or []) if e]
        import yfinance as yf
        spy = yf.Ticker("SPY").history(period="3mo", interval="1d")

        def spy_pct(bars: int) -> float:
            bars = min(bars, len(spy) - 1)
            if bars <= 0:
                return 0.0
            return (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-1 - bars]) - 1) * 100

        specs = [("1-day", d["prev_close"], 1), ("5-day", eq[-5] if len(eq) >= 5 else None, 5),
                 ("20-day", eq[-20] if len(eq) >= 20 else None, 20),
                 ("Since start", START_EQUITY, len(spy) - 1)]
        for label, prev, bars in specs:
            if not prev:
                continue
            bot = (d["equity"] / prev - 1) * 100
            sp = spy_pct(bars)
            d["windows"].append({"label": label, "chg": d["equity"] - prev,
                                 "bot_pct": bot, "spy_pct": sp, "edge": bot - sp})
    except Exception as e:
        d["warnings"].append(f"benchmark windows unavailable ({e})")

    # ── POSITIONS: broker only ────────────────────────────────────────
    try:
        ps = requests.get(f"{PAPER_API}/v2/positions", headers=_hdr(), timeout=15).json()
        d["positions"] = [{
            "symbol": p["symbol"], "qty": float(p["qty"]),
            "is_option": len(p["symbol"]) > 12,
            "side": "LONG" if float(p["qty"]) > 0 else "SHORT",
            "mv": float(p["market_value"]), "unrl": float(p["unrealized_pl"]),
        } for p in ps]
        d["unrealized"] = sum(p["unrl"] for p in d["positions"])
        d["gross_exposure"] = sum(abs(p["mv"]) for p in d["positions"])
        d["leverage"] = (d["gross_exposure"] / d["equity"]) if d["equity"] else 0
    except Exception as e:
        d["warnings"].append(f"positions unavailable ({e})")
        d["positions"], d["unrealized"] = [], None

    # ── ATTRIBUTION: ledger only (never money-of-record) ──────────────
    try:
        import trade_ledger as _tl
        today = d["today"]
        opened = [t for t in _tl.trades_on_date(today) ]
        closed = [t for t in _tl.all_trades()
                  if not t.is_open and (t.exit_at_et or "")[:10] == today]
        d["opened_today"] = [{
            "time": (t.opened_at_et or "")[11:19], "symbol": t.symbol,
            "side": t.side,
            "agent": str(t.primary_agent).replace("MetaAgent(", "").rstrip(")"),
            "notional": float(t.entry_price or 0) * float(t.shares or 0),
        } for t in opened]
        d["closed_today"] = [{
            "symbol": t.symbol, "side": t.side, "pnl": t.realized_pnl or 0.0,
            "agent": str(t.primary_agent).replace("MetaAgent(", "").rstrip(")"),
        } for t in closed]
        d["ledger_realized_today"] = sum(c["pnl"] for c in d["closed_today"])
    except Exception as e:
        d["warnings"].append(f"ledger attribution unavailable ({e})")
        d["opened_today"], d["closed_today"] = [], []
        d["ledger_realized_today"] = None

    # ── SELF-VALIDATION — the point of this module ────────────────────
    # Ledger realized + change in open marks should roughly equal the
    # broker's day P&L. A large gap means positions exist that the ledger
    # cannot see (the drift that hid $19,908 of losses). Say so loudly.
    if d.get("ledger_realized_today") is not None and d.get("day_pnl") is not None:
        gap = abs(d["day_pnl"] - d["ledger_realized_today"])
        d["reconcile_gap"] = gap
        if gap > RECONCILE_TOLERANCE:
            d["warnings"].append(
                f"RECONCILE: broker day P&L ${d['day_pnl']:+,.0f} vs ledger realized "
                f"${d['ledger_realized_today']:+,.0f} — ${gap:,.0f} unexplained. "
                f"Broker figure is authoritative; the gap is open-position marks "
                f"and/or positions missing from the ledger.")

    # Positions the ledger does not know about — the drift that has
    # repeatedly consumed buying power invisibly.
    try:
        import trade_ledger as _tl
        led = {t.symbol.replace("/", "") for t in _tl.open_positions()}
        orphans = [p["symbol"] for p in d.get("positions", [])
                   if not p["is_option"] and p["symbol"] not in led]
        d["orphans"] = orphans
        if orphans:
            d["warnings"].append(
                f"DRIFT: broker holds {len(orphans)} position(s) the ledger has "
                f"closed: {', '.join(orphans[:8])}")
    except Exception:
        d["orphans"] = []

    if d.get("leverage", 0) > 2.0:
        d["warnings"].append(f"LEVERAGE {d['leverage']:.2f}x — gross exposure "
                             f"${d['gross_exposure']:,.0f} on ${d['equity']:,.0f} equity")
    if d.get("buying_power", 1e9) < 20_000:
        d["warnings"].append(f"BUYING POWER ${d['buying_power']:,.0f} — near the "
                             f"reserve floor; new entries will be blocked")
    return d


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = snapshot()
    print(f"equity      ${s['equity']:,.2f}   day {s['day_pnl']:+,.2f}   "
          f"total {s['total_pct']:+.2f}%")
    print(f"positions   {len(s['positions'])}  unrealized ${s['unrealized']:+,.2f}  "
          f"leverage {s.get('leverage',0):.2f}x")
    print(f"opened today {len(s['opened_today'])}   closed today {len(s['closed_today'])}")
    for w in s["windows"]:
        print(f"  {w['label']:12} bot {w['bot_pct']:+6.2f}%  spy {w['spy_pct']:+6.2f}%  "
              f"edge {w['edge']:+6.2f}%")
    print("\nWARNINGS:" if s["warnings"] else "\nno warnings")
    for w in s["warnings"]:
        print(f"  ⚠ {w}")
