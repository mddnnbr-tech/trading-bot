"""
trade_context.py
─────────────────
The "why" store. SQLite because causal learning needs joins and
aggregates across conditions, which a flat CSV cannot do.

The ledger records WHAT happened (symbol, P&L). That is not learnable —
knowing GOOGL lost $863 teaches nothing actionable. This module records
the CONDITIONS at entry and then classifies HOW each trade died, which
converts outcomes into root causes:

    "stopped out, price recovered to target within 2 days"
        -> the stop was too tight (fixable: widen ATR multiple)
    "gapped through the stop overnight"
        -> overnight exposure (fixable: prefer options / close intraday)
    "never moved, died on time decay"
        -> no catalyst behind the signal (fixable: raise conviction bar)
    "went against immediately and never recovered"
        -> the entry signal had no edge (not fixable by exits — bench it)

Those four have different remedies. Without classification every loss
looks identical and the only available "learning" is to trade less.

Tables
  entries     one row per trade at submission: full condition snapshot
  postmortems one row per closed trade: failure/success mode + evidence

Nothing here places orders. It is a memory, and a set of queries that
turn that memory into parameter changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("TradeContext")

DB_PATH = Path(__file__).resolve().parent / "data" / "trade_context.db"

# Failure/success modes. Each maps to a DIFFERENT remedy — that is the
# whole point of classifying rather than counting.
MODE_REMEDY = {
    "STOP_TOO_TIGHT":  "widen ATR stop multiple",
    "GAP_LOSS":        "reduce overnight equity exposure; prefer options",
    "NO_CATALYST":     "raise conviction bar; require news/volume confirmation",
    "WRONG_DIRECTION": "entry signal lacks edge — reduce agent weight",
    "GAVE_BACK":       "tighten trailing stop; winner reversed before exit",
    "WIN_TRAILED":     "working as designed",
    "WIN_TARGET":      "working as designed",
    "UNCLASSIFIED":    "insufficient data",
}


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS entries(
        trade_key TEXT PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT,
        agent TEXT, agent_count INTEGER, confidence REAL, raw_confidence REAL,
        entry_price REAL, stop_price REAL, target_price REAL,
        atr_pct REAL, regime TEXT, vix REAL, hour_et INTEGER,
        instrument TEXT, reasons TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS postmortems(
        trade_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, agent TEXT,
        exit_ts TEXT, pnl REAL, mode TEXT, evidence TEXT,
        recovered_to_target INTEGER, gapped INTEGER, days_held REAL)""")
    return c


def record_entry(signal: dict, regime: str = "", vix: float = 0.0,
                 instrument: str = "equity") -> None:
    """Snapshot the conditions at submission. Cheap, never raises."""
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        key = f"{signal.get('symbol')}|{signal.get('direction')}|{ts[:16]}"
        entry = float(signal.get("entry_price") or 0)
        stop = float(signal.get("stop_loss_price") or 0)
        atr_pct = (abs(entry - stop) / entry * 100) if entry else 0.0
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                key, ts, signal.get("symbol"), signal.get("direction"),
                str(signal.get("agent", ""))[:120],
                int(signal.get("agent_count", 1) or 1),
                float(signal.get("confidence") or 0),
                float(signal.get("raw_confidence") or signal.get("confidence") or 0),
                entry, stop, float(signal.get("target_price") or 0),
                round(atr_pct, 2), regime, vix,
                datetime.now().hour, instrument,
                json.dumps(signal.get("reasons", []))[:600],
            ))
    except Exception as e:
        log.debug(f"context: entry record failed: {e}")


def _price_window(symbol: str, start: str, days: int = 5):
    """Daily bars for the N days AFTER exit — needed to tell a bad stop
    from a bad thesis."""
    try:
        import yfinance as yf
        sym = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD",
               "SOL/USD": "SOL-USD"}.get(symbol, symbol)
        d0 = datetime.fromisoformat(start[:19])
        df = yf.Ticker(sym).history(start=d0.strftime("%Y-%m-%d"),
                                    end=(d0 + timedelta(days=days)).strftime("%Y-%m-%d"),
                                    interval="1d")
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def classify_closed_trades(limit: int = 60) -> dict:
    """Post-mortem every closed trade not yet classified.

    The decisive test: after we exited at a loss, did price reach the
    original target within 5 days? If yes the thesis was RIGHT and the
    stop was wrong — a completely different fix from a thesis that was
    simply wrong.
    """
    import trade_ledger as _tl
    counts: dict[str, int] = {}
    try:
        with _conn() as c:
            done = {r[0] for r in c.execute("SELECT trade_key FROM postmortems")}
            closed = [t for t in _tl.epoch_trades() if not t.is_open]
            for t in closed[-limit:]:
                key = f"{t.symbol}|{t.side}|{(t.opened_at_et or '')[:16]}"
                if key in done:
                    continue
                pnl = t.realized_pnl or 0.0
                exit_ts = t.exit_at_et or t.opened_at_et or ""
                try:
                    held = (datetime.fromisoformat(exit_ts[:19])
                            - datetime.fromisoformat(t.opened_at_et[:19])).total_seconds() / 86400
                except Exception:
                    held = 0.0

                mode, evidence, recovered, gapped = "UNCLASSIFIED", "", 0, 0
                if pnl > 0:
                    mode = "WIN_TARGET" if t.status == "target" else "WIN_TRAILED"
                    evidence = f"exit {t.status}, held {held:.1f}d"
                else:
                    df = _price_window(t.symbol, exit_ts, 5)
                    tgt = t.target_price or 0
                    if df is not None and tgt:
                        hi, lo = float(df["High"].max()), float(df["Low"].min())
                        recovered = int(hi >= tgt) if t.side == "LONG" else int(lo <= tgt)
                    # Gap: exit filled well beyond the stop level
                    try:
                        if t.exit_price and t.stop_price:
                            slip = abs(float(t.exit_price) - float(t.stop_price)) / float(t.stop_price) * 100
                            gapped = int(slip > 2.0)
                    except Exception:
                        pass

                    if gapped:
                        mode = "GAP_LOSS"
                        evidence = "exit filled >2% beyond stop — overnight gap"
                    elif recovered:
                        mode = "STOP_TOO_TIGHT"
                        evidence = "price reached original target within 5d of exit"
                    elif held < 0.4 and abs(pnl) < 60:
                        mode = "NO_CATALYST"
                        evidence = f"died in {held*24:.0f}h with little movement"
                    else:
                        mode = "WRONG_DIRECTION"
                        evidence = f"moved against and never recovered ({held:.1f}d)"

                c.execute("INSERT OR REPLACE INTO postmortems VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    key, t.symbol, t.side,
                    str(t.primary_agent).replace("MetaAgent(", "").rstrip(")").split(",")[0].strip(),
                    exit_ts, round(pnl, 2), mode, evidence, recovered, gapped, round(held, 2)))
                counts[mode] = counts.get(mode, 0) + 1
    except Exception as e:
        log.warning(f"context: classification failed: {e}")
    if counts:
        log.info(f"🔬 Post-mortem classified {sum(counts.values())}: {counts}")
    return counts


def failure_summary(days: int = 20) -> list[dict]:
    """Aggregate root causes with their P&L impact and prescribed remedy."""
    cut = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT mode, COUNT(*), ROUND(SUM(pnl),2) FROM postmortems "
                "WHERE exit_ts >= ? GROUP BY mode ORDER BY SUM(pnl)", (cut,)).fetchall()
        return [{"mode": m, "count": n, "pnl": p,
                 "remedy": MODE_REMEDY.get(m, "")} for m, n, p in rows]
    except Exception:
        return []


def recommended_adjustments(days: int = 20) -> dict:
    """Turn root causes into concrete parameter changes.

    Only fires on a dominant, well-evidenced pattern — a single bad trade
    must never move a global parameter.
    """
    s = failure_summary(days)
    total = sum(x["count"] for x in s) or 1
    losses = {x["mode"]: x for x in s if x["pnl"] < 0}
    out: dict = {}
    for mode, x in losses.items():
        share = x["count"] / total
        if share < 0.25 or x["count"] < 5:
            continue
        if mode == "STOP_TOO_TIGHT":
            out["atr_stop_mult"] = "increase 1.5 -> 2.0"
        elif mode == "GAP_LOSS":
            out["overnight_policy"] = "route more signals to options (defined risk)"
        elif mode == "NO_CATALYST":
            out["min_solo_confidence"] = "increase 0.65 -> 0.70"
        elif mode == "WRONG_DIRECTION":
            out["agent_weighting"] = "entry edge lacking — lower weights / bench worst agents"
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("classifying...", classify_closed_trades(limit=200))
    print("\nROOT CAUSES (20d):")
    for r in failure_summary():
        print(f"  {r['mode']:16} n={r['count']:3d}  pnl=${r['pnl']:+9.2f}   -> {r['remedy']}")
    print("\nRECOMMENDED:", recommended_adjustments() or "no dominant pattern yet")
