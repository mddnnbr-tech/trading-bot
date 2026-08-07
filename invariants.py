"""
invariants.py
──────────────
Continuous assertions about what must ALWAYS be true.

Why this exists. Four reporting bugs shipped in one week, all silent, all
flattering, and every one violated a rule nobody was checking:

  ledger P&L vs broker equity        $22,000 error   (nothing compared them)
  dead trade_log.jsonl as source     trades vanished (nothing checked freshness)
  history index read as "today"      $6,960 error    (nothing compared to live)
  expiry booked open gains as real   $11,003 error   (nothing checked double-count)

The structural cause is visible in one line: FIVE modules can close a
position (crypto_scheduler, ensemble, market_scheduler, options_executor,
trade_ledger) and SEVEN can compute P&L. Whenever two systems decide the
same thing independently, they eventually disagree — and without an
assertion, the disagreement is silent.

This module cannot fix that architecture. What it does is make any
divergence LOUD and IMMEDIATE, so the next one is caught in an hour
rather than after it has distorted a week of decisions.

Two layers:
  1. INVARIANTS  — explicit rules. Catch known failure shapes.
  2. ANOMALIES   — statistical. Catch shapes nobody has thought of yet,
                   by flagging any metric far outside its own history.

Severity: CRITICAL alerts immediately, WARN goes in the report, INFO logs.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
log = logging.getLogger("Invariants")
HISTORY = BASE / "data" / "invariant_history.jsonl"
PAPER_API = "https://paper-api.alpaca.markets"


def _hdr():
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}


def check_all() -> list[dict]:
    """Run every invariant. Returns violations, most severe first."""
    v: list[dict] = []

    def fail(sev, rule, detail, fix=""):
        v.append({"severity": sev, "rule": rule, "detail": detail, "fix": fix})

    # ── Gather state once ─────────────────────────────────────────────
    try:
        acct = requests.get(f"{PAPER_API}/v2/account", headers=_hdr(), timeout=15).json()
        positions = requests.get(f"{PAPER_API}/v2/positions", headers=_hdr(), timeout=15).json()
        orders = requests.get(f"{PAPER_API}/v2/orders", headers=_hdr(),
                              params={"status": "open", "limit": 200}, timeout=15).json()
    except Exception as e:
        fail("CRITICAL", "broker_reachable", f"cannot read broker: {e}",
             "everything below is unverifiable until this clears")
        return v

    try:
        import trade_ledger as _tl
        ledger_open = {t.symbol.replace("/", ""): t for t in _tl.open_positions()}
        all_trades = _tl.all_trades()
    except Exception as e:
        fail("CRITICAL", "ledger_readable", f"cannot read ledger: {e}")
        return v

    equity = float(acct.get("equity") or 0)
    broker_syms = {p["symbol"] for p in positions}
    equities = [p for p in positions if len(p["symbol"]) <= 12]

    # ── 1. No position may be closed in the ledger while the broker
    #      holds it. Violated 4x this week; source of every phantom gain.
    orphans = broker_syms - set(ledger_open)
    if orphans:
        fail("WARN", "no_orphan_positions",
             f"broker holds {len(orphans)} position(s) the ledger closed: "
             f"{', '.join(sorted(orphans)[:8])}",
             "trade_ledger.sync_from_broker()")

    # ── 2. …and the reverse: the ledger must not claim a position the
    #      broker does not have.
    ghosts = set(ledger_open) - broker_syms
    if ghosts:
        fail("WARN", "no_ghost_positions",
             f"ledger shows {len(ghosts)} open the broker does not hold: "
             f"{', '.join(sorted(ghosts)[:8])}",
             "these inflate perceived exposure; close them in the ledger")

    # ── 3. No trade may be counted realized AND still open (the $11,003
    #      double-count). Same symbol closed today yet held now.
    today = datetime.now().strftime("%Y-%m-%d")
    # Only NON-ZERO realized P&L inflates a report. A row closed flat
    # during reconciliation shares the shape but causes no harm — flagging
    # it would train us to ignore this rule, which is worse than the bug.
    dbl = [t.symbol for t in all_trades
           if not t.is_open and (t.exit_at_et or "")[:10] == today
           and abs(t.realized_pnl or 0.0) > 1.0
           and t.symbol.replace("/", "") in broker_syms]
    if dbl:
        fail("CRITICAL", "no_double_counted_pnl",
             f"{len(dbl)} symbol(s) booked as realized today while still held: "
             f"{', '.join(sorted(set(dbl))[:8])} — reported profit is inflated",
             "re-open those rows; the gain is unrealized")

    # ── 4. Every equity position needs a live exit order, or it is
    #      unprotected — no stop, unbounded downside.
    # Crypto is excluded by design: Alpaca supports no exit orders on
    # crypto, so manage_crypto_exits() polls every 15 min instead. That
    # substitution is only valid while the poller is actually running —
    # checked separately below rather than assumed.
    def _is_crypto(sym): return sym.endswith("USD") and len(sym) > 5
    protected = {o["symbol"] for o in orders}
    naked = [p["symbol"] for p in equities
             if p["symbol"] not in protected and not _is_crypto(p["symbol"])]
    if naked:
        fail("CRITICAL", "all_positions_protected",
             f"{len(naked)} position(s) have NO exit order: "
             f"{', '.join(sorted(naked)[:8])} — unbounded downside",
             "resubmit trailing stops")

    # Crypto's protection IS the poller. If it stops, those positions are
    # silently unprotected with nothing at the broker to catch them.
    if any(_is_crypto(p["symbol"]) for p in positions):
        try:
            cl = BASE / "logs" / "crypto_scheduler.log"
            age_min = (datetime.now().timestamp() - cl.stat().st_mtime) / 60
            if age_min > 45:
                fail("CRITICAL", "crypto_exit_poller_alive",
                     f"crypto positions open but the exit poller has not run in "
                     f"{age_min:.0f} min — they have NO protection of any kind",
                     "check the */15 crypto_scheduler cron")
        except Exception as e:
            fail("WARN", "crypto_exit_poller_alive", f"cannot verify poller: {e}")

    # ── 5. One position per symbol. Duplicate stacking cost -$460/day
    #      in July and froze the account twice.
    from collections import Counter
    dupes = {s: n for s, n in Counter(p["symbol"] for p in positions).items() if n > 1}
    if dupes:
        fail("CRITICAL", "no_duplicate_positions",
             f"stacked positions: {dupes}", "dedup gate failed")

    # ── 6. Leverage ceiling. 2.43x turned a -2.5% day into -15%.
    gross = sum(abs(float(p["market_value"])) for p in positions)
    lev = gross / equity if equity else 0
    if lev > 2.5:
        fail("CRITICAL", "leverage_ceiling", f"leverage {lev:.2f}x (gross ${gross:,.0f})",
             "reduce exposure — this is how the account nearly died")
    elif lev > 2.0:
        fail("WARN", "leverage_ceiling", f"leverage {lev:.2f}x approaching limit")

    # ── 7. Buying power must stay above the reserve or entries silently
    #      fail — 597 doomed submissions in one day.
    bp = float(acct.get("buying_power") or 0)
    if bp < 5_000:
        fail("CRITICAL", "buying_power_floor", f"buying power ${bp:,.0f}",
             "orders will bounce; free capital")
    elif bp < 20_000:
        fail("WARN", "buying_power_floor", f"buying power ${bp:,.0f} near reserve")

    # ── 8. Options premium is the max loss — no option may exceed the
    #      per-trade risk budget. Stacking broke this once ($2,150 on an
    #      $890 budget).
    opts = [p for p in positions if len(p["symbol"]) > 12]
    budget = equity * 0.015
    over = [f"{p['symbol']}(${abs(float(p['cost_basis'])):,.0f})" for p in opts
            if abs(float(p.get("cost_basis") or 0)) > budget]
    if over:
        fail("WARN", "options_within_budget",
             f"option cost basis over ${budget:,.0f} budget: {', '.join(over[:5])}")

    v.sort(key=lambda x: 0 if x["severity"] == "CRITICAL" else 1)
    _record({"ts": datetime.now().isoformat(timespec="seconds"),
             "equity": equity, "leverage": round(lev, 3), "bp": bp,
             "positions": len(positions), "orphans": len(orphans),
             "violations": len(v)})
    return v


def _record(metrics: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(metrics) + "\n")


def anomalies(z: float = 3.0) -> list[dict]:
    """Catch failure shapes nobody has thought of yet.

    Invariants only catch rules we already know to state. Every bug this
    week was a rule we had NOT stated. This is the complement: flag any
    tracked metric that lands far outside its own recent distribution,
    which surfaces novel breakage without needing to predict it first.
    """
    try:
        rows = [json.loads(l) for l in HISTORY.read_text().splitlines()[-400:]]
    except Exception:
        return []
    if len(rows) < 30:
        return []

    out = []
    for key in ("equity", "leverage", "bp", "positions", "orphans"):
        series = [r[key] for r in rows[:-1] if isinstance(r.get(key), (int, float))]
        latest = rows[-1].get(key)
        if len(series) < 30 or latest is None:
            continue
        mu = statistics.mean(series)
        sd = statistics.pstdev(series)
        if sd < 1e-9:
            continue
        score = abs(latest - mu) / sd
        if score >= z:
            out.append({"severity": "WARN", "rule": f"anomaly:{key}",
                        "detail": f"{key}={latest:,.2f} is {score:.1f} sigma from its "
                                  f"{len(series)}-sample mean {mu:,.2f} — unusual, "
                                  f"investigate before trusting today's figures",
                        "fix": ""})
    return out


def report() -> list[dict]:
    return check_all() + anomalies()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    issues = report()
    if not issues:
        print("✅ all invariants hold")
    for i in issues:
        print(f"[{i['severity']}] {i['rule']}: {i['detail']}"
              + (f"\n         fix: {i['fix']}" if i["fix"] else ""))
