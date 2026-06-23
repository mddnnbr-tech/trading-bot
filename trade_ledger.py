"""
trade_ledger.py — v1.1 (2026-04-24)
───────────────────────────────────
Single source of truth for paper-trade P&L.

Bridges the gap between what the bot ACTUALLY logs (PAPER TRADE lines in
scheduler.log) and what the reporter needs to read (a structured ledger).

Architecture:
  scheduler.log  ──[parse_log()]──▶  paper_trades.csv (the ledger)
       │                                      │
       │                                      ├──▶ daily reporter (today's slice)
       │                                      ├──▶ portfolio view (cumulative)
       │                                      └──▶ agent evaluator (per-agent)
       │
       └─ refresh_open_positions() updates current price + checks target/stop hits

CSV schema (one row per trade):
  trade_id, opened_at_et, symbol, side, primary_agent, contributors,
  entry_price, target_price, stop_price, risk_dollar, shares,
  status, exit_price, exit_at_et, exit_reason,
  realized_pnl, unrealized_pnl, current_price, last_updated_et

Status values:
  open      — position still active, target & stop not yet hit
  target    — target price reached → realized win
  stop      — stop price reached → realized loss
  expired   — held > MAX_HOLD_DAYS without hit → closed at current price

Idempotency: trade_id = sha1(opened_at_et + symbol + side + entry_price)[:12]
Re-running parse_log() never duplicates a row.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).resolve().parent
LOGS_DIR  = BASE_DIR / "logs"
DATA_DIR  = BASE_DIR / "data"
LEDGER    = DATA_DIR / "paper_trades.csv"
SCHEDLOG  = LOGS_DIR / "scheduler.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")

# ── Tunables ─────────────────────────────────────────────────────────────────
DEFAULT_RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "320"))  # $ per trade
MAX_HOLD_DAYS          = int(os.getenv("MAX_HOLD_DAYS", "5"))       # auto-expire after N

LONG_SIDES  = {"LONG", "BUY", "CALL"}
SHORT_SIDES = {"SHORT", "SELL", "PUT"}

CSV_FIELDS = [
    "trade_id", "opened_at_et", "symbol", "side",
    "primary_agent", "contributors",
    "entry_price", "target_price", "stop_price",
    "risk_dollar", "shares",
    "status", "exit_price", "exit_at_et", "exit_reason",
    "realized_pnl", "unrealized_pnl", "current_price", "last_updated_et",
]


# ── Regex — bot's PAPER TRADE log line format ───────────────────────────────
# Example:
#   2026-04-24 10:06:09,237 [INFO] PAPER TRADE: META SHORT entry=$663.05 \
#       target=$616.64 stop=$679.63 agent=MetaAgent(ShortMomentumAgent)
#
# Real production logs prepend an emoji (📋, 📓, etc.) between [INFO] and
# PAPER TRADE, so we tolerate any optional non-alphanumeric prefix token.
PAPER_TRADE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})[,\.]\d+\s+"
    r"\[INFO\]\s+(?:[^\w\s]+\s+)?PAPER\s+TRADE:\s+"
    r"(?P<symbol>[A-Z][A-Z0-9\.\-]*)\s+"
    r"(?P<side>[A-Z]+)\s+"
    r"entry=\$?(?P<entry>[\d\.]+)\s+"
    r"target=\$?(?P<target>[\d\.]+)\s+"
    r"stop=\$?(?P<stop>[\d\.]+)\s+"
    r"agent=(?P<agent>.+?)\s*$"
)


@dataclass
class Trade:
    trade_id:       str
    opened_at_et:   str
    symbol:         str
    side:           str               # LONG | SHORT (normalized)
    primary_agent:  str
    contributors:   str               # comma-joined sub-agents
    entry_price:    float
    target_price:   float
    stop_price:     float
    risk_dollar:    float
    shares:         float
    status:         str = "open"      # open | target | stop | expired
    exit_price:     Optional[float] = None
    exit_at_et:     str = ""
    exit_reason:    str = ""
    realized_pnl:   float = 0.0
    unrealized_pnl: float = 0.0
    current_price:  Optional[float] = None
    last_updated_et: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def all_agents(self) -> list[str]:
        agents = [self.primary_agent]
        if self.contributors:
            agents += [a.strip() for a in self.contributors.split(",") if a.strip()]
        return agents

    @property
    def opened_date_et(self) -> str:
        return self.opened_at_et[:10]


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _normalize_side(raw_side: str) -> str:
    s = raw_side.upper().strip()
    if s in SHORT_SIDES: return "SHORT"
    if s in LONG_SIDES:  return "LONG"
    return s  # leave unknowns alone for visibility


def _parse_agent_field(agent_raw: str) -> tuple[str, str]:
    """Split MetaAgent(SubA, SubB) → ('MetaAgent', 'SubA, SubB').
    Plain 'TechnicalAgent' → ('TechnicalAgent', '')."""
    agent_raw = agent_raw.strip()
    m = re.match(r"^([A-Za-z_]+)\s*\(([^)]*)\)\s*$", agent_raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return agent_raw, ""


def _trade_id(opened_at_et: str, symbol: str, side: str, entry: float) -> str:
    seed = f"{opened_at_et}|{symbol}|{side}|{entry:.4f}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def _shares_for_risk(risk_dollar: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    return round(risk_dollar / entry, 4)


def parse_paper_trade_line(line: str) -> Optional[Trade]:
    """Parse one scheduler.log line; return Trade or None if not a paper trade line."""
    m = PAPER_TRADE_RE.match(line.rstrip())
    if not m:
        return None
    ts        = m.group("ts")
    symbol    = m.group("symbol")
    side      = _normalize_side(m.group("side"))
    entry     = float(m.group("entry"))
    target    = float(m.group("target"))
    stop      = float(m.group("stop"))
    primary, contribs = _parse_agent_field(m.group("agent"))
    risk      = DEFAULT_RISK_PER_TRADE
    shares    = _shares_for_risk(risk, entry)
    return Trade(
        trade_id      = _trade_id(ts, symbol, side, entry),
        opened_at_et  = ts,
        symbol        = symbol,
        side          = side,
        primary_agent = primary,
        contributors  = contribs,
        entry_price   = entry,
        target_price  = target,
        stop_price    = stop,
        risk_dollar   = risk,
        shares        = shares,
    )


# ── Ledger I/O ───────────────────────────────────────────────────────────────

def load_ledger() -> dict[str, Trade]:
    """Read the CSV ledger into {trade_id: Trade}."""
    if not LEDGER.exists():
        return {}
    out: dict[str, Trade] = {}
    with LEDGER.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out[row["trade_id"]] = Trade(
                    trade_id        = row["trade_id"],
                    opened_at_et    = row["opened_at_et"],
                    symbol          = row["symbol"],
                    side            = row["side"],
                    primary_agent   = row["primary_agent"],
                    contributors    = row.get("contributors", ""),
                    entry_price     = float(row["entry_price"]),
                    target_price    = float(row["target_price"]),
                    stop_price      = float(row["stop_price"]),
                    risk_dollar     = float(row["risk_dollar"]),
                    shares          = float(row["shares"]),
                    status          = row.get("status", "open"),
                    exit_price      = float(row["exit_price"]) if row.get("exit_price") else None,
                    exit_at_et      = row.get("exit_at_et", ""),
                    exit_reason     = row.get("exit_reason", ""),
                    realized_pnl    = float(row.get("realized_pnl") or 0),
                    unrealized_pnl  = float(row.get("unrealized_pnl") or 0),
                    current_price   = float(row["current_price"]) if row.get("current_price") else None,
                    last_updated_et = row.get("last_updated_et", ""),
                )
            except (KeyError, ValueError) as e:
                # tolerate corrupt rows; skip
                continue
    return out


def save_ledger(trades: dict[str, Trade]) -> None:
    """Atomic write of the entire ledger."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        # Sort by opened time so the file is human-readable
        for t in sorted(trades.values(), key=lambda x: x.opened_at_et):
            row = asdict(t)
            # Normalize None → "" for CSV
            for k, v in row.items():
                if v is None:
                    row[k] = ""
            writer.writerow(row)
    tmp.replace(LEDGER)


# ── Backfill from scheduler.log ──────────────────────────────────────────────

def parse_log(log_path: Path = SCHEDLOG) -> tuple[int, int]:
    """Scan scheduler.log; merge any new PAPER TRADE entries into the ledger.
    Returns (newly_added, total_in_ledger). Idempotent."""
    if not log_path.exists():
        return (0, 0)
    existing = load_ledger()
    added = 0
    try:
        with log_path.open(errors="ignore") as f:
            for line in f:
                t = parse_paper_trade_line(line)
                if t is None:
                    continue
                if t.trade_id not in existing:
                    existing[t.trade_id] = t
                    added += 1
    except OSError:
        return (0, len(existing))
    if added:
        save_ledger(existing)
    return (added, len(existing))


# ── Open-position resolver (target / stop / expiry checks) ───────────────────

def _import_yf():
    try:
        import yfinance as yf  # type: ignore
        return yf
    except ImportError:
        return None


def _fetch_price_path(symbol: str, since_iso_et: str, yf) -> Optional[object]:
    """Return a DataFrame of price bars from `since` through now, or None."""
    try:
        opened_dt = datetime.fromisoformat(since_iso_et)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=ET)
    except Exception:
        return None
    days_held = max(1, (datetime.now(ET) - opened_dt).days + 1)
    period    = f"{min(days_held + 2, 60)}d"
    interval  = "5m" if days_held <= 5 else "1d"
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        # Localize index to ET
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(ET)
        else:
            df.index = df.index.tz_convert(ET)
        # Trim to bars at/after entry
        df = df[df.index >= opened_dt]
        return df
    except Exception:
        return None


def _check_hits(trade: Trade, df) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """Walk price bars in time order. First bar that touches target/stop wins.
    Returns (status, exit_price, exit_at_et) or (None, None, None) if still open."""
    if df is None or df.empty:
        return (None, None, None)
    is_long = trade.side == "LONG"
    for ts, row in df.iterrows():
        hi = float(row.get("High", row.get("Close", 0)))
        lo = float(row.get("Low",  row.get("Close", 0)))
        if is_long:
            if hi >= trade.target_price:
                return ("target", trade.target_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
            if lo <= trade.stop_price:
                return ("stop", trade.stop_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
        else:  # SHORT
            if lo <= trade.target_price:
                return ("target", trade.target_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
            if hi >= trade.stop_price:
                return ("stop", trade.stop_price, ts.strftime("%Y-%m-%d %H:%M:%S"))
    return (None, None, None)


def _pnl_for(trade: Trade, exit_price: float) -> float:
    sign = 1 if trade.side == "LONG" else -1
    return round(trade.shares * (exit_price - trade.entry_price) * sign, 2)


def refresh_open_positions(max_symbols: int = 60) -> dict:
    """For every open trade, fetch price path, mark hits, update unrealized P&L.
    Returns summary dict for logging."""
    yf = _import_yf()
    if yf is None:
        return {"error": "yfinance not installed", "updated": 0}

    trades = load_ledger()
    open_trades = [t for t in trades.values() if t.is_open]
    if not open_trades:
        save_ledger(trades)  # touch file even if nothing open
        return {"checked": 0, "closed_target": 0, "closed_stop": 0, "expired": 0, "still_open": 0}

    # Group by symbol so we minimize yfinance calls
    by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in open_trades:
        by_symbol[t.symbol].append(t)

    closed_target = closed_stop = expired = still_open = 0
    now_et = datetime.now(ET)
    now_iso = now_et.strftime("%Y-%m-%d %H:%M:%S")

    # Cap the work per run to avoid timeouts on huge backfills
    symbols_to_process = list(by_symbol.keys())[:max_symbols]

    for symbol in symbols_to_process:
        # Fetch once per symbol — earliest open trade dictates start
        earliest = min(by_symbol[symbol], key=lambda t: t.opened_at_et)
        df = _fetch_price_path(symbol, earliest.opened_at_et, yf)
        last_price = None
        if df is not None and not df.empty:
            try:
                last_price = float(df["Close"].iloc[-1])
            except Exception:
                last_price = None

        for t in by_symbol[symbol]:
            # Filter df to bars at/after this trade's open
            t_df = None
            if df is not None and not df.empty:
                try:
                    opened_dt = datetime.fromisoformat(t.opened_at_et).replace(tzinfo=ET)
                    t_df = df[df.index >= opened_dt]
                except Exception:
                    t_df = df
            status, exit_price, exit_at = _check_hits(t, t_df)

            if status:
                t.status        = status
                t.exit_price    = exit_price
                t.exit_at_et    = exit_at
                t.exit_reason   = "target hit" if status == "target" else "stop hit"
                t.realized_pnl  = _pnl_for(t, exit_price)
                t.unrealized_pnl = 0.0
                if status == "target": closed_target += 1
                else:                  closed_stop   += 1
            else:
                # Still open — check expiry
                try:
                    opened_dt = datetime.fromisoformat(t.opened_at_et).replace(tzinfo=ET)
                    age_days  = (now_et - opened_dt).days
                except Exception:
                    age_days = 0
                if age_days >= MAX_HOLD_DAYS and last_price is not None:
                    t.status         = "expired"
                    t.exit_price     = last_price
                    t.exit_at_et     = now_iso
                    t.exit_reason    = f"expired after {age_days}d"
                    t.realized_pnl   = _pnl_for(t, last_price)
                    t.unrealized_pnl = 0.0
                    expired += 1
                else:
                    if last_price is not None:
                        t.current_price  = round(last_price, 2)
                        t.unrealized_pnl = _pnl_for(t, last_price)
                    still_open += 1

            t.last_updated_et = now_iso
            trades[t.trade_id] = t

    save_ledger(trades)
    return {
        "checked":        len(open_trades),
        "closed_target":  closed_target,
        "closed_stop":    closed_stop,
        "expired":        expired,
        "still_open":     still_open,
        "symbols_seen":   len(symbols_to_process),
    }


# ── Query API (used by the reporter) ─────────────────────────────────────────

def all_trades() -> list[Trade]:
    return sorted(load_ledger().values(), key=lambda t: t.opened_at_et)


def trades_on_date(date_str: str) -> list[Trade]:
    """date_str: 'YYYY-MM-DD' in ET"""
    return [t for t in all_trades() if t.opened_date_et == date_str]


def open_positions() -> list[Trade]:
    return [t for t in all_trades() if t.is_open]


def closed_trades() -> list[Trade]:
    return [t for t in all_trades() if not t.is_open]


def daily_pnl_series() -> list[dict]:
    """Group ALL trades by opened-date. Return [{date, realized, unrealized, count}]."""
    by_date: dict[str, dict] = defaultdict(lambda: {
        "realized": 0.0, "unrealized": 0.0, "count": 0, "wins": 0, "losses": 0,
    })
    for t in all_trades():
        d = by_date[t.opened_date_et]
        d["count"] += 1
        d["realized"]   += t.realized_pnl
        d["unrealized"] += t.unrealized_pnl
        pnl = t.realized_pnl if not t.is_open else t.unrealized_pnl
        if pnl >= 0: d["wins"]   += 1
        else:        d["losses"] += 1
    out = []
    for date in sorted(by_date.keys()):
        d = by_date[date]
        d["date"]     = date
        d["total"]    = round(d["realized"] + d["unrealized"], 2)
        d["realized"] = round(d["realized"], 2)
        d["unrealized"] = round(d["unrealized"], 2)
        out.append(d)
    return out


def cumulative_pnl() -> dict:
    """Lifetime aggregates: total P&L, trade count, win rate, best/worst day."""
    series = daily_pnl_series()
    trades = all_trades()
    if not trades:
        return {
            "total_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "trade_count": 0, "open_count": 0, "closed_count": 0,
            "wins": 0, "losses": 0, "win_rate": 0.0,
            "best_day": None, "worst_day": None,
            "first_trade_date": None, "last_trade_date": None,
            "trading_days": 0,
        }
    realized   = sum(t.realized_pnl for t in trades)
    unrealized = sum(t.unrealized_pnl for t in trades)
    closed     = [t for t in trades if not t.is_open]
    wins       = sum(1 for t in closed if t.realized_pnl >= 0)
    losses     = len(closed) - wins
    best  = max(series, key=lambda d: d["total"]) if series else None
    worst = min(series, key=lambda d: d["total"]) if series else None
    return {
        "total_pnl":       round(realized + unrealized, 2),
        "realized_pnl":    round(realized, 2),
        "unrealized_pnl":  round(unrealized, 2),
        "trade_count":     len(trades),
        "open_count":      sum(1 for t in trades if t.is_open),
        "closed_count":    len(closed),
        "wins":            wins,
        "losses":          losses,
        "win_rate":        round(wins / len(closed) * 100, 1) if closed else 0.0,
        "best_day":        best,
        "worst_day":       worst,
        "first_trade_date": trades[0].opened_date_et,
        "last_trade_date":  trades[-1].opened_date_et,
        "trading_days":    len(series),
    }


def per_agent_attribution() -> list[dict]:
    """For each agent (primary + contributors counted), return aggregate stats."""
    by_agent: dict[str, dict] = defaultdict(lambda: {
        "agent": "", "trades_total": 0, "trades_open": 0, "trades_closed": 0,
        "wins": 0, "losses": 0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0,
        "as_primary": 0, "as_contributor": 0,
        "best_trade": None, "worst_trade": None,
    })
    for t in all_trades():
        for i, agent in enumerate(t.all_agents):
            d = by_agent[agent]
            d["agent"] = agent
            d["trades_total"] += 1
            if i == 0: d["as_primary"]    += 1
            else:      d["as_contributor"] += 1
            if t.is_open:
                d["trades_open"]    += 1
                d["unrealized_pnl"] += t.unrealized_pnl
            else:
                d["trades_closed"]  += 1
                d["realized_pnl"]   += t.realized_pnl
                if t.realized_pnl >= 0: d["wins"]   += 1
                else:                   d["losses"] += 1
            pnl = t.realized_pnl if not t.is_open else t.unrealized_pnl
            if d["best_trade"] is None or pnl > d["best_trade"]["pnl"]:
                d["best_trade"] = {
                    "symbol": t.symbol, "side": t.side, "pnl": round(pnl, 2),
                    "date": t.opened_date_et,
                }
            if d["worst_trade"] is None or pnl < d["worst_trade"]["pnl"]:
                d["worst_trade"] = {
                    "symbol": t.symbol, "side": t.side, "pnl": round(pnl, 2),
                    "date": t.opened_date_et,
                }
    out = []
    for d in by_agent.values():
        total_pnl = d["realized_pnl"] + d["unrealized_pnl"]
        d["total_pnl"]    = round(total_pnl, 2)
        d["realized_pnl"] = round(d["realized_pnl"], 2)
        d["unrealized_pnl"] = round(d["unrealized_pnl"], 2)
        if d["trades_closed"] > 0:
            d["win_rate"]   = round(d["wins"] / d["trades_closed"] * 100, 1)
            d["avg_pnl"]    = round(d["realized_pnl"] / d["trades_closed"], 2)
        else:
            d["win_rate"]   = 0.0
            d["avg_pnl"]    = 0.0
        out.append(d)
    # Rank by total_pnl descending (best first)
    return sorted(out, key=lambda x: -x["total_pnl"])


# ── CLI / cron entrypoint ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print(f"[trade_ledger] base_dir = {BASE_DIR}")
    print(f"[trade_ledger] scheduler log = {SCHEDLOG} (exists: {SCHEDLOG.exists()})")
    print(f"[trade_ledger] ledger file = {LEDGER}")

    added, total = parse_log()
    print(f"[trade_ledger] parse_log: +{added} new trades  ({total} total in ledger)")

    if "--refresh-prices" in sys.argv or "--refresh" in sys.argv:
        print("[trade_ledger] refreshing open positions...")
        result = refresh_open_positions()
        print(f"[trade_ledger] {result}")

    if "--summary" in sys.argv:
        cum = cumulative_pnl()
        print("\n=== CUMULATIVE ===")
        for k, v in cum.items():
            print(f"  {k:20} {v}")
        print("\n=== PER-AGENT (top 10 by P&L) ===")
        for row in per_agent_attribution()[:10]:
            print(f"  {row['agent']:25} trades={row['trades_total']:4} "
                  f"pnl=${row['total_pnl']:>+10.2f}  win%={row['win_rate']:>5.1f}")
