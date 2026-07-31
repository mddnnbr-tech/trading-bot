"""
daily_postmortem.py
────────────────────
Nightly learning job. Runs after the close, classifies every trade that
closed today, ranks the day's best and worst, and writes a dated
markdown report the user can read in OneDrive.

This is the piece that makes the loop closed rather than open: the
classifier turns outcomes into root causes, and this job puts those root
causes in front of a human every day with the prescribed remedy attached.

Cron:  0 17 * * 1-5  cd /home/mddnnbr/tading-bot && python3 daily_postmortem.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import trade_context
import trade_ledger as _tl

ET = ZoneInfo("America/New_York")
OUT_DIR = Path(__file__).resolve().parent / "analysis"


def build_report() -> str:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    trade_context.classify_closed_trades(limit=400)

    db = sqlite3.connect(trade_context.DB_PATH)
    rows = db.execute(
        "SELECT symbol, side, agent, pnl, mode, evidence, days_held "
        "FROM postmortems WHERE exit_ts LIKE ? ORDER BY pnl DESC",
        (f"{today}%",)).fetchall()

    L = [f"# Post-Mortem — {today}", ""]

    if not rows:
        L.append("_No trades closed today._")
    else:
        wins = [r for r in rows if r[3] > 0]
        loss = [r for r in rows if r[3] <= 0]
        net = sum(r[3] for r in rows)
        L += [f"**{len(rows)} closed** · {len(wins)}W / {len(loss)}L · "
              f"net **${net:+,.2f}**", "",
              "| Symbol | Side | Agent | P&L | Mode | Why | Held |",
              "|---|---|---|---:|---|---|---:|"]
        for sym, side, ag, pnl, mode, ev, held in rows:
            L.append(f"| {sym} | {side} | {ag[:18]} | ${pnl:+,.0f} | "
                     f"{mode} | {ev} | {held:.1f}d |")

    # Rolling root-cause table — the actionable part
    L += ["", "## Root causes (20 sessions)", "",
          "| Mode | Trades | P&L | Remedy |", "|---|---:|---:|---|"]
    for r in trade_context.failure_summary(20):
        L.append(f"| {r['mode']} | {r['count']} | ${r['pnl']:+,.2f} | {r['remedy']} |")

    # Which agent owns which failure — drives bench/weight decisions
    L += ["", "## Agent × failure mode (20 sessions)", "",
          "| Agent | Mode | Trades | P&L |", "|---|---|---:|---:|"]
    cut = (datetime.now() - timedelta(days=20)).isoformat()
    for ag, mode, n, p in db.execute(
            "SELECT agent, mode, COUNT(*), ROUND(SUM(pnl),0) FROM postmortems "
            "WHERE exit_ts >= ? GROUP BY agent, mode ORDER BY SUM(pnl) LIMIT 14", (cut,)):
        L.append(f"| {ag} | {mode} | {n} | ${p:+,.0f} |")

    rec = trade_context.recommended_adjustments(20)
    L += ["", "## Recommended parameter changes", ""]
    L += ([f"- **{k}** → {v}" for k, v in rec.items()] if rec
          else ["- _No dominant pattern (needs ≥25% share and ≥5 trades)._"])

    # Equity truth — broker, never the ledger
    try:
        import os, requests
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
        h = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
             "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}
        eq = [e for e in (requests.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history",
            params={"period": "1M", "timeframe": "1D"}, headers=h,
            timeout=15).json().get("equity") or []) if e]
        if eq:
            L += ["", "## Account (broker equity)", "",
                  f"- Equity **${eq[-1]:,.0f}** ({(eq[-1]/100000-1)*100:+.2f}% since $100k)",
                  f"- Day {eq[-1]-eq[-2]:+,.0f}" if len(eq) > 1 else ""]
    except Exception:
        pass

    db.close()
    return "\n".join(L)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    body = build_report()
    path = OUT_DIR / f"postmortem_{today}.md"
    path.write_text(body, encoding="utf-8")
    (OUT_DIR / "latest.md").write_text(body, encoding="utf-8")
    print(f"written → {path}")


if __name__ == "__main__":
    main()
