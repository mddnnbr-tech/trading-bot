"""
condition_analysis.py
──────────────────────
Level 2: which CONDITIONS predict which OUTCOMES.

Level 1 (trade_context) answers "how did this trade die". That is a
taxonomy, not a model — it cannot tell you what to avoid tomorrow. This
module joins entry conditions to outcomes and asks the questions that
actually change behaviour:

  • Does conviction predict success? If a 0.80 signal wins no more often
    than a 0.60 signal, every confidence gate in the system is theatre.
  • Does consensus help? If 2-agent trades beat solo trades, tighten the
    solo bar. If not, the corroboration rules cost us trades for nothing.
  • Does time of day matter? The first 30 minutes is widely held to be
    noise — this measures whether that is true for US.
  • Does volatility regime matter? High VIX may deserve smaller size or
    no overnight holds at all.

Backfills conditions from the ledger for historical trades so the
analysis works today rather than in six weeks. Backfilled rows carry
what the ledger knows (symbol, side, agent, prices, hour, ATR%) plus
reconstructed VIX from history; fields that were never recorded are left
null and simply excluded from those cuts.

Reports EVIDENCE, never mutates parameters. auto_tune remains the only
writer, so there is exactly one path to a live change.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import trade_context

log = logging.getLogger("ConditionAnalysis")
BASE = Path(__file__).resolve().parent
MIN_CELL = 6          # never draw a conclusion from fewer than this many trades


def backfill_entries() -> int:
    """Reconstruct entry conditions for trades that closed before the
    context store existed."""
    import trade_ledger as _tl
    added = 0
    vix_hist = {}
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX").history(period="6mo", interval="1d")
        vix_hist = {str(i.date()): float(c) for i, c in zip(v.index, v["Close"])}
    except Exception:
        pass

    with trade_context._conn() as c:
        have = {r[0] for r in c.execute("SELECT trade_key FROM entries")}
        for t in _tl.epoch_trades():
            key = f"{t.symbol}|{t.side}|{(t.opened_at_et or '')[:16]}"
            if key in have or not t.opened_at_et:
                continue
            try:
                dt = datetime.fromisoformat(t.opened_at_et[:19])
            except Exception:
                continue
            entry = float(t.entry_price or 0)
            stop = float(t.stop_price or 0)
            atr_pct = (abs(entry - stop) / entry * 100) if entry else 0.0
            agent = str(t.primary_agent).replace("MetaAgent(", "").rstrip(")")
            n_agents = len([a for a in agent.split(",") if a.strip()])
            c.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                key, t.opened_at_et, t.symbol,
                "long" if t.side == "LONG" else "short",
                agent.split(",")[0].strip()[:120], n_agents,
                0.0, 0.0, entry, stop, float(t.target_price or 0),
                round(atr_pct, 2), "", vix_hist.get(str(dt.date()), 0.0),
                dt.hour, "equity", ""))
            added += 1
    if added:
        log.info(f"backfilled {added} historical entry rows")
    return added


def _concentration(c, expr: str, bucket) -> float:
    """Share of a bucket's absolute P&L coming from its top 2 trades.

    Added after the 2026-07-31 near-miss: the '9-10am is our profitable
    window' finding was 81% two trades, one of which was a manual
    liquidation of an orphan position — not the strategy working. Any
    bucket whose result rides on a couple of trades must be labelled, or
    it will be acted on as though it were a pattern.
    """
    rows = [abs(r[0]) for r in c.execute(
        f"SELECT m.pnl FROM entries e JOIN postmortems m ON e.trade_key=m.trade_key "
        f"WHERE {expr} = ? ORDER BY ABS(m.pnl) DESC", (bucket,)).fetchall()]
    tot = sum(rows)
    return (sum(rows[:2]) / tot * 100) if tot else 0.0


def _cut(c, expr: str, label: str) -> list[str]:
    """Win rate and avg P&L sliced by an arbitrary SQL expression."""
    rows = c.execute(f"""
        SELECT {expr} AS bucket,
               COUNT(*),
               ROUND(AVG(CASE WHEN m.pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 0),
               ROUND(AVG(m.pnl), 0),
               ROUND(SUM(m.pnl), 0)
        FROM entries e JOIN postmortems m ON e.trade_key = m.trade_key
        GROUP BY bucket ORDER BY bucket""").fetchall()
    out = [f"### {label}", "", "| bucket | n | win% | avg P&L | total | reliability |",
           "|---|---:|---:|---:|---:|---|"]
    seen = False
    for b, n, wr, avg, tot in rows:
        if n < MIN_CELL:
            continue
        seen = True
        conc = _concentration(c, expr, b)
        flag = ("⚠️ top-2 trades = {:.0f}% — NOT a pattern".format(conc)
                if conc >= 60 else "ok ({:.0f}% top-2)".format(conc))
        out.append(f"| {b} | {n} | {wr:.0f}% | ${avg:+,.0f} | ${tot:+,.0f} | {flag} |")
    if not seen:
        out.append(f"| _no bucket has {MIN_CELL}+ trades yet_ | | | | | |")
    return out + [""]


def analyze() -> str:
    trade_context.classify_closed_trades(limit=500)
    backfill_entries()
    c = sqlite3.connect(trade_context.DB_PATH)

    joined = c.execute("SELECT COUNT(*) FROM entries e "
                       "JOIN postmortems m ON e.trade_key=m.trade_key").fetchone()[0]
    L = ["# Condition → Outcome Analysis", "",
         f"_{joined} trades with both conditions and classified outcomes. "
         f"Buckets under {MIN_CELL} trades are suppressed as noise._", ""]

    L += _cut(c, "e.agent_count", "Consensus — does agreement help?")
    L += _cut(c, "CASE WHEN e.hour_et < 10 THEN '1. open (9-10am)' "
                 "WHEN e.hour_et < 12 THEN '2. late morning' "
                 "WHEN e.hour_et < 14 THEN '3. midday' "
                 "ELSE '4. afternoon' END", "Time of day — is the open really noise?")
    L += _cut(c, "CASE WHEN e.vix <= 0 THEN 'unknown' WHEN e.vix < 16 THEN '1. calm (<16)' "
                 "WHEN e.vix < 22 THEN '2. normal (16-22)' ELSE '3. stressed (22+)' END",
              "Volatility regime")
    L += _cut(c, "e.side", "Direction — long vs short")
    L += _cut(c, "CASE WHEN e.atr_pct < 2 THEN '1. tight (<2%)' "
                 "WHEN e.atr_pct < 4 THEN '2. medium (2-4%)' ELSE '3. wide (4%+)' END",
              "Stop width at entry")
    L += _cut(c, "CASE WHEN e.confidence <= 0 THEN 'unrecorded' "
                 "WHEN e.confidence < 0.6 THEN '1. low (<0.60)' "
                 "WHEN e.confidence < 0.7 THEN '2. mid (0.60-0.70)' "
                 "ELSE '3. high (0.70+)' END",
              "Conviction — does confidence predict anything?")

    # Holding period is the clearest lever we have evidence for
    L += ["### Holding period vs outcome", "",
          "| days held | n | win% | avg P&L |", "|---|---:|---:|---:|"]
    for b, n, wr, avg in c.execute("""
        SELECT CASE WHEN m.days_held < 0.5 THEN '1. intraday'
                    WHEN m.days_held < 2 THEN '2. 1-2 days'
                    WHEN m.days_held < 5 THEN '3. 2-5 days'
                    ELSE '4. 5+ days' END,
               COUNT(*), ROUND(AVG(CASE WHEN m.pnl>0 THEN 1.0 ELSE 0.0 END)*100,0),
               ROUND(AVG(m.pnl),0)
        FROM postmortems m GROUP BY 1 ORDER BY 1"""):
        if n >= MIN_CELL:
            L.append(f"| {b} | {n} | {wr:.0f}% | ${avg:+,.0f} |")
    L.append("")

    c.close()
    return "\n".join(L)


def main():
    out_dir = BASE / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    body = analyze()
    (out_dir / "conditions.md").write_text(body, encoding="utf-8")
    print(body)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
