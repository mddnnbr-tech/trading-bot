"""
daily_reporter.py — v2.1 (2026-04-22)
─────────────────────────────────────
Daily trading performance email, fired at 4:35 PM ET on weekdays.

v2.1 — adds segmented Performance Tracking (Paper/Test vs. Actual/Live):
  • Shadow P&L engine — calculates "what would my P&L be if every approved
    signal had filled at signal time and was still held now". Pulls entry
    prices from yfinance at signal timestamp; current price from yfinance
    last close. Surfaces winners/losers and per-agent shadow P&L.
  • Forward-compatible LIVE column — pre-wired to read from
    `logs/live_fills.jsonl` when Phase B / live trading is wired. Today
    it's a STANDBY placeholder; on go-live it auto-populates without
    further code changes.
  • Mode badge — "PAPER (TEST) ● ACTIVE" vs. "ACTUAL (LIVE) ○ STANDBY"
    based on TRADING_MODE / PAPER_TRADING env vars.

v2 — fixed the critical "report shows 0 trades while bot fires 50+/day" bug:
  • Reads the actual `event` field (SIGNAL_APPROVED / SIGNAL_REJECTED) in
    trade_log.jsonl. v1 looked for a nonexistent `status` field → always 0.
  • Properly converts trade_log UTC timestamps → ET before date-matching.
  • Parses scheduler.log directly for: tick count, fetch errors, raw signal
    counts, MetaAgent synthesis output.
  • Sections: Approved Trades, Rejection Reasons, Per-Agent Activity,
    Fetch Errors, Tick Health, System Diagnosis.

Cron entry (no change required):
    35 16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/daily_reporter.py --send-now
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

from agent_evaluator import AgentEvaluator
from performance_logger import PerformanceLogger, LOGS_DIR

# v2.2 — bring in the new structured ledger as the source of truth for paper P&L.
# trade_ledger is OPTIONAL — if the import fails, the report falls back to v2.1
# behavior so an upload glitch on the VM doesn't break the email entirely.
try:
    import trade_ledger as _ledger
    _LEDGER_AVAILABLE = True
except Exception:
    _ledger = None
    _LEDGER_AVAILABLE = False

# Always load .env from THIS script's directory, regardless of cron's CWD.
# Bug fix 2026-04-23: cron runs with CWD=~/, not ~/tading-bot/, so plain
# load_dotenv() couldn't find the .env and Gmail creds came back empty,
# silently failing every automated 4:35 PM run while manual tests worked.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
ET = ZoneInfo("America/New_York")

GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PW    = os.getenv("GMAIL_APP_PASSWORD", "")
REPORT_TO_EMAIL = os.getenv("REPORT_TO_EMAIL", GMAIL_ADDRESS)

# Trading mode — drives whether PAPER or LIVE column is the "active" one in the report.
# Defaults to PAPER when PAPER_TRADING is true (or unset). Override with TRADING_MODE=live.
_PAPER_FLAG = os.getenv("PAPER_TRADING", "true").strip().lower() in ("1", "true", "yes")
TRADING_MODE = os.getenv("TRADING_MODE", "paper" if _PAPER_FLAG else "live").strip().lower()

APPROVED_EVENTS = {"SIGNAL_APPROVED", "approved", "APPROVED"}
REJECTED_EVENTS = {"SIGNAL_REJECTED", "rejected", "REJECTED"}

LONG_DIRECTIONS  = {"long", "buy", "call", "calls", "bullish", "bull"}
SHORT_DIRECTIONS = {"short", "sell", "put", "puts", "bearish", "bear"}


# ── Time helpers ─────────────────────────────────────────────────────────────

def _today_et() -> datetime:
    return datetime.now(ET)


def _today_str() -> str:
    return _today_et().strftime("%Y-%m-%d")


def _to_et_date_str(ts_str: str) -> str:
    """Convert ANY ISO timestamp string to ET-local date 'YYYY-MM-DD'."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET).strftime("%Y-%m-%d")
    except Exception:
        return ts_str[:10]


def _to_et_time_str(ts_str: str) -> str:
    """Convert ISO timestamp to ET time 'HH:MM:SS'."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET).strftime("%H:%M:%S")
    except Exception:
        return ""


# ── Data readers ─────────────────────────────────────────────────────────────

def read_trade_log_today() -> list[dict]:
    """Return today's entries from trade_log.jsonl, ET-date-filtered."""
    path = LOGS_DIR / "trade_log.jsonl"
    if not path.exists():
        return []
    today = _today_str()
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if _to_et_date_str(rec.get("timestamp", "")) == today:
                entries.append(rec)
        except json.JSONDecodeError:
            continue
    return entries


def read_trade_log_recent_per_agent(days: int = 14) -> dict:
    """Return {agent_name: most_recent_signal_iso} across last N days.
    Used for 'last seen' silence detection."""
    path = LOGS_DIR / "trade_log.jsonl"
    if not path.exists():
        return {}
    cutoff = (_today_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    last_seen: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            d = _to_et_date_str(rec.get("timestamp", ""))
            if d < cutoff:
                continue
            agent = rec.get("agent", "")
            if not agent:
                continue
            if agent not in last_seen or d > _to_et_date_str(last_seen[agent]):
                last_seen[agent] = rec.get("timestamp", "")
        except json.JSONDecodeError:
            continue
    return last_seen


def read_scheduler_today() -> dict:
    """Parse scheduler.log for today's tick health, errors, raw signal counts."""
    path = LOGS_DIR / "scheduler.log"
    today = _today_str()
    result = {
        "ran": False,
        "first_log": None,
        "last_log":  None,
        "tick_count": 0,
        "error_count": 0,
        "fetch_404_symbols": defaultdict(int),
        "raw_signal_ticks": [],     # list of (time_str, total_count)
        "synthesis_events": [],     # list of (time_str, raw, passed)
        "approved_batches": [],     # list of (time_str, count)
        "agent_signal_counts": defaultdict(int),  # cumulative across all today's ticks
        "regime_observations": [],  # list of regime strings observed
    }
    if not path.exists():
        return result

    try:
        content = path.read_text(errors="ignore")
    except Exception:
        return result

    today_lines = [l for l in content.splitlines() if today in l]
    if not today_lines:
        return result

    result["ran"] = True
    result["first_log"] = today_lines[0][:19]
    result["last_log"]  = today_lines[-1][:19]

    re_404_sym = re.compile(r"symbol:\s*([A-Z][A-Z0-9\-]*)")
    re_short_404 = re.compile(r"\[ERROR\]\s+([A-Z][A-Z0-9\-]*):\s*No earnings")
    re_raw_signals = re.compile(r"Total raw signals from all agents:\s*(\d+)")
    re_synthesis = re.compile(r"(\d+)\s+raw signals\s*→\s*(\d+)\s+passed synthesis")
    re_approved_batch = re.compile(r"Tick produced\s+(\d+)\s+approved signal")
    re_agent_count = re.compile(r"(\w+Agent):\s*(\d+)\s*signal")
    re_regime = re.compile(r"active regimes\s*=\s*\{([^}]+)\}")

    for line in today_lines:
        time_str = line[11:19] if len(line) > 19 else ""

        if "[ERROR]" in line:
            result["error_count"] += 1
            m = re_404_sym.search(line) or re_short_404.search(line)
            if m:
                result["fetch_404_symbols"][m.group(1)] += 1

        if "Ensemble cycle start" in line:
            result["tick_count"] += 1

        m = re_raw_signals.search(line)
        if m:
            result["raw_signal_ticks"].append((time_str, int(m.group(1))))

        m = re_synthesis.search(line)
        if m:
            result["synthesis_events"].append((time_str, int(m.group(1)), int(m.group(2))))

        m = re_approved_batch.search(line)
        if m:
            result["approved_batches"].append((time_str, int(m.group(1))))

        m = re_agent_count.search(line)
        if m:
            result["agent_signal_counts"][m.group(1)] += int(m.group(2))

        m = re_regime.search(line)
        if m:
            result["regime_observations"].append(m.group(1).strip())

    return result


def read_open_positions() -> list[dict]:
    for name in ("open_positions.json", "positions.json", "portfolio.json"):
        path = LOGS_DIR / name
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "positions" in data:
                    return data["positions"]
            except Exception:
                continue
    return []


# ── Shadow P&L engine (Paper / Test) ─────────────────────────────────────────
#
# "Shadow" because no fills actually happened — we simulate what the P&L *would*
# be if every approved signal had filled at the signal-time price and was still
# held at last close. Caveats:
#   • Assumes perfect fills at signal price (no slippage, no spread)
#   • Assumes positions held until now (no stops, no profit targets)
#   • Treats every signal as stock-equivalent — option leverage NOT modeled.
#     For options/calls/puts the directional sign is correct but the dollar
#     P&L is conservative (real options would amplify the move via delta).
#
# Replace this engine with read_live_fills() output once Phase B is wired.

def _empty_pnl_summary(note: str = "") -> dict:
    return {
        "active":         False,
        "positions":      [],
        "total_pnl":      0.0,
        "total_notional": 0.0,
        "win_count":      0,
        "loss_count":     0,
        "win_rate":       0.0,
        "by_agent":       {},
        "biggest_winner": None,
        "biggest_loser":  None,
        "tickers_priced": 0,
        "tickers_failed": 0,
        "note":           note,
    }


def _safe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _batch_current_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch last close for each symbol via yfinance. Returns {symbol: price}."""
    prices: dict[str, float] = {}
    for sym in symbols:
        if not sym or sym == "—":
            continue
        try:
            hist = yf.Ticker(sym).history(period="2d", interval="1d")
            if hist is not None and not hist.empty:
                prices[sym] = float(hist["Close"].iloc[-1])
        except Exception:
            continue
    return prices


def _intraday_entry_price(symbol: str, timestamp_iso: str,
                          cache: dict[str, object]) -> float | None:
    """Look up the 5-min bar closest to a signal's timestamp for a fill estimate."""
    if not timestamp_iso:
        return None
    if symbol not in cache:
        try:
            cache[symbol] = yf.Ticker(symbol).history(period="1d", interval="5m")
        except Exception:
            cache[symbol] = None
    bars = cache.get(symbol)
    if bars is None or bars.empty:
        return None
    try:
        signal_dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        if signal_dt.tzinfo is None:
            signal_dt = signal_dt.replace(tzinfo=ET)
        signal_dt_et = signal_dt.astimezone(ET)
        if bars.index.tz is None:
            bars.index = bars.index.tz_localize("UTC").tz_convert(ET)
        else:
            bars.index = bars.index.tz_convert(ET)
        # Closest 5m bar to the signal time
        deltas = [abs((idx - signal_dt_et).total_seconds()) for idx in bars.index]
        i = deltas.index(min(deltas))
        return float(bars["Close"].iloc[i])
    except Exception:
        return None


def compute_shadow_pnl(approved_signals: list[dict]) -> dict:
    """Calculate shadow P&L for today's approved signals.

    Returns a summary dict with positions, totals, per-agent breakdown,
    biggest winner/loser. Always returns a structured dict (never raises).
    """
    if not approved_signals:
        return _empty_pnl_summary("No approved signals to simulate.")

    symbols = sorted({s.get("symbol") for s in approved_signals
                      if s.get("symbol") and s.get("symbol") != "—"})
    if not symbols:
        return _empty_pnl_summary("Approved signals had no parseable symbols.")

    current_prices = _batch_current_prices(symbols)
    intraday_cache: dict[str, object] = {}

    positions: list[dict] = []
    total_pnl = 0.0
    total_notional = 0.0
    wins = losses = 0
    by_agent: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    biggest_winner = None
    biggest_loser  = None

    for s in approved_signals:
        symbol = s.get("symbol")
        if not symbol or symbol == "—":
            continue
        current_price = current_prices.get(symbol)
        if current_price is None or current_price <= 0:
            continue

        risk_f = _safe_float(s.get("risk_dollar") or s.get("risk"))
        if not risk_f or risk_f <= 0:
            continue

        # Entry price — prefer signal-supplied; fall back to intraday yfinance bar
        entry_price = (
            _safe_float(s.get("entry_price"))
            or _safe_float(s.get("price"))
            or _safe_float(s.get("entry"))
            or _intraday_entry_price(symbol, s.get("timestamp", ""), intraday_cache)
            or current_price  # ultimate fallback → P&L = 0 for this position
        )
        if entry_price <= 0:
            continue

        direction = str(s.get("direction") or s.get("side") or "long").lower()
        if direction in SHORT_DIRECTIONS:
            sign = -1
        else:
            sign = 1  # default long if ambiguous

        shares = risk_f / entry_price
        pnl    = shares * (current_price - entry_price) * sign
        pnl_pct = (pnl / risk_f * 100) if risk_f else 0.0

        total_pnl      += pnl
        total_notional += risk_f
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

        agent = s.get("agent", "Unknown")
        by_agent[agent]["pnl"]   += pnl
        by_agent[agent]["count"] += 1

        position = {
            "time":      _to_et_time_str(s.get("timestamp", "")),
            "symbol":    symbol,
            "direction": direction,
            "agent":     agent,
            "entry":     round(entry_price, 2),
            "current":   round(current_price, 2),
            "shares":    round(shares, 2),
            "notional":  round(risk_f, 2),
            "pnl":       round(pnl, 2),
            "pnl_pct":   round(pnl_pct, 2),
        }
        positions.append(position)

        if biggest_winner is None or pnl > biggest_winner["pnl"]:
            biggest_winner = position
        if biggest_loser is None or pnl < biggest_loser["pnl"]:
            biggest_loser = position

    total = wins + losses
    return {
        "active":         True,
        "positions":      sorted(positions, key=lambda p: -p["pnl"]),
        "total_pnl":      round(total_pnl, 2),
        "total_notional": round(total_notional, 2),
        "win_count":      wins,
        "loss_count":     losses,
        "win_rate":       round(wins / total * 100, 1) if total else 0.0,
        "by_agent":       {a: {"pnl": round(d["pnl"], 2), "count": d["count"]}
                           for a, d in by_agent.items()},
        "biggest_winner": biggest_winner,
        "biggest_loser":  biggest_loser,
        "tickers_priced": len(current_prices),
        "tickers_failed": len([s for s in symbols if s not in current_prices]),
        "note":           "",
    }


# ── Ledger-backed P&L (v2.2) ─────────────────────────────────────────────────
#
# This replaces compute_shadow_pnl as the primary Paper-side P&L source.
# Reads from data/paper_trades.csv (the new structured ledger) which is
# populated by trade_ledger.parse_log() pulling from scheduler.log.
#
# Why a second function: compute_shadow_pnl reads from trade_log.jsonl which
# the bot doesn't write to. The bot logs "PAPER TRADE:" lines straight into
# scheduler.log. trade_ledger bridges that gap.

def _ledger_summary_from_trades(trade_list, label_for_empty: str) -> dict:
    """Convert a list of trade_ledger.Trade objects into the dict shape the
    existing _format_pnl_column / _format_performance_panel expect."""
    if not trade_list:
        return _empty_pnl_summary(label_for_empty)

    positions = []
    total_pnl = 0.0
    total_notional = 0.0
    wins = losses = 0
    by_agent = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    biggest_winner = None
    biggest_loser  = None

    for t in trade_list:
        # For open trades, P&L is unrealized; for closed, it's realized.
        pnl = t.realized_pnl if not t.is_open else t.unrealized_pnl
        notional = t.risk_dollar
        total_pnl += pnl
        total_notional += notional
        if pnl >= 0: wins += 1
        else:        losses += 1

        # Attribute to primary agent for the by-agent breakdown
        by_agent[t.primary_agent]["pnl"]   += pnl
        by_agent[t.primary_agent]["count"] += 1

        # Direction string compatible with the existing renderer
        dir_str = "long" if t.side == "LONG" else "short"

        # Display: time = HH:MM:SS from opened_at_et
        try:
            time_str = t.opened_at_et[11:19]
        except Exception:
            time_str = ""

        current_for_display = t.exit_price if not t.is_open else (t.current_price or t.entry_price)

        position = {
            "time":      time_str,
            "symbol":    t.symbol,
            "direction": dir_str,
            "agent":     t.primary_agent,
            "entry":     round(t.entry_price, 2),
            "current":   round(float(current_for_display), 2),
            "shares":    round(t.shares, 4),
            "notional":  round(notional, 2),
            "pnl":       round(pnl, 2),
            "pnl_pct":   round(pnl / notional * 100, 2) if notional else 0.0,
            "status":    t.status,        # extra fields the new sections use
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
        }
        positions.append(position)

        if biggest_winner is None or pnl > biggest_winner["pnl"]:
            biggest_winner = position
        if biggest_loser is None or pnl < biggest_loser["pnl"]:
            biggest_loser = position

    total = wins + losses
    return {
        "active":         True,
        "positions":      sorted(positions, key=lambda p: -p["pnl"]),
        "total_pnl":      round(total_pnl, 2),
        "total_notional": round(total_notional, 2),
        "win_count":      wins,
        "loss_count":     losses,
        "win_rate":       round(wins / total * 100, 1) if total else 0.0,
        "by_agent":       {a: {"pnl": round(d["pnl"], 2), "count": d["count"]}
                           for a, d in by_agent.items()},
        "biggest_winner": biggest_winner,
        "biggest_loser":  biggest_loser,
        "tickers_priced": len({p["symbol"] for p in positions}),
        "tickers_failed": 0,
        "note":           "",
    }


def compute_paper_pnl_from_ledger() -> dict:
    """Today's paper P&L — read from data/paper_trades.csv."""
    if not _LEDGER_AVAILABLE:
        return _empty_pnl_summary("trade_ledger module not loaded — upload trade_ledger.py to the VM.")
    today = _today_str()
    todays = _ledger.trades_on_date(today)
    return _ledger_summary_from_trades(todays, f"No paper trades opened today ({today}).")


def _ledger_position_to_dict(t) -> dict:
    """Lightweight serialization of a trade_ledger.Trade for the open-positions table."""
    pnl = t.unrealized_pnl if t.is_open else t.realized_pnl
    return {
        "opened_at_et":  t.opened_at_et,
        "symbol":        t.symbol,
        "side":          t.side,
        "primary_agent": t.primary_agent,
        "contributors":  t.contributors,
        "entry":         round(t.entry_price, 2),
        "target":        round(t.target_price, 2),
        "stop":          round(t.stop_price, 2),
        "current":       round(float(t.current_price), 2) if t.current_price else None,
        "shares":        round(t.shares, 4),
        "notional":      round(t.risk_dollar, 2),
        "status":        t.status,
        "exit_price":    round(float(t.exit_price), 2) if t.exit_price else None,
        "exit_reason":   t.exit_reason,
        "pnl":           round(pnl, 2),
        "pnl_pct":       round(pnl / t.risk_dollar * 100, 2) if t.risk_dollar else 0.0,
    }


# ── Live P&L reader (Actual / Live) — pre-wired for Phase B ──────────────────
#
# When Phase B (the paper-execution shim) is built, it should write filled
# trades to logs/live_fills.jsonl with one JSON record per fill containing:
#   {"timestamp":"...", "symbol":"AAPL", "direction":"long", "shares":10,
#    "entry":182.0, "exit":185.0, "realized_pnl":30.0, "status":"closed"}
#
# Until then, this returns an empty/STANDBY summary so the report still
# renders the column with a clear "not yet wired" note.

def read_live_fills() -> dict:
    path = LOGS_DIR / "live_fills.jsonl"
    if not path.exists():
        return _empty_pnl_summary(
            "Live execution not wired yet — Phase B (order execution shim) pending. "
            "This column will auto-populate once live_fills.jsonl exists."
        )

    today = _today_str()
    fills: list[dict] = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if _to_et_date_str(rec.get("timestamp", "")) == today:
                fills.append(rec)
        except json.JSONDecodeError:
            continue

    if not fills:
        return _empty_pnl_summary("No live fills today.")

    positions: list[dict] = []
    total_pnl = 0.0
    total_notional = 0.0
    wins = losses = 0
    by_agent: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    biggest_winner = None
    biggest_loser  = None

    for f in fills:
        pnl   = _safe_float(f.get("realized_pnl") or f.get("pnl")) or 0.0
        risk  = _safe_float(f.get("notional") or f.get("risk_dollar")) or 0.0
        agent = f.get("agent", "Unknown")
        by_agent[agent]["pnl"]   += pnl
        by_agent[agent]["count"] += 1
        total_pnl      += pnl
        total_notional += risk
        if pnl >= 0:
            wins += 1
        else:
            losses += 1
        position = {
            "time":      _to_et_time_str(f.get("timestamp", "")),
            "symbol":    f.get("symbol", "—"),
            "direction": f.get("direction", "—"),
            "agent":     agent,
            "entry":     _safe_float(f.get("entry")) or 0.0,
            "current":   _safe_float(f.get("exit"))  or 0.0,
            "shares":    _safe_float(f.get("shares")) or 0.0,
            "notional":  risk,
            "pnl":       round(pnl, 2),
            "pnl_pct":   round(pnl / risk * 100, 2) if risk else 0.0,
        }
        positions.append(position)
        if biggest_winner is None or pnl > biggest_winner["pnl"]:
            biggest_winner = position
        if biggest_loser is None or pnl < biggest_loser["pnl"]:
            biggest_loser = position

    total = wins + losses
    return {
        "active":         True,
        "positions":      sorted(positions, key=lambda p: -p["pnl"]),
        "total_pnl":      round(total_pnl, 2),
        "total_notional": round(total_notional, 2),
        "win_count":      wins,
        "loss_count":     losses,
        "win_rate":       round(wins / total * 100, 1) if total else 0.0,
        "by_agent":       {a: {"pnl": round(d["pnl"], 2), "count": d["count"]}
                           for a, d in by_agent.items()},
        "biggest_winner": biggest_winner,
        "biggest_loser":  biggest_loser,
        "tickers_priced": len(fills),
        "tickers_failed": 0,
        "note":           "",
    }


# ── Diagnostic engine ────────────────────────────────────────────────────────

def diagnose(report: dict) -> list[str]:
    """Return a list of human-readable findings about today's behavior."""
    findings = []
    sched = report["sched"]

    if not sched["ran"]:
        findings.append("⚠️  CRITICAL: scheduler.log has no entries for today — "
                        "the bot didn't run. Check `systemctl status trading-bot`.")
        return findings

    expected_ticks = 390  # 6.5h × 60 ticks/h
    if sched["tick_count"] < 50:
        findings.append(
            f"⚠️  Only {sched['tick_count']} ticks today vs. ~{expected_ticks} expected. "
            f"Bot may be hung on yfinance fetches or agent loops. "
            f"Check `tail -200 scheduler.log` for stuck imports/timeouts."
        )

    if sched["error_count"] > 100:
        top_404 = sorted(sched["fetch_404_symbols"].items(), key=lambda x: -x[1])[:5]
        top_str = ", ".join(f"{s} ({n}×)" for s, n in top_404)
        findings.append(
            f"⚠️  {sched['error_count']} errors today, mostly yfinance 404s. "
            f"Worst offenders: {top_str}. EarningsAgent likely querying ETFs/crypto "
            f"that don't have earnings — blacklist them from its universe."
        )

    if report["approved_count"] == 0 and report["rejected_count"] == 0 and report["raw_signals_total"] > 0:
        findings.append(
            f"⚠️  {report['raw_signals_total']} raw signals fired but 0 approved/rejected "
            f"in trade_log.jsonl. Either the synthesis layer isn't writing decisions, "
            f"or the file path is wrong."
        )

    if report["approved_count"] == 0 and report["rejected_count"] > 0:
        # Check rejection reasons for tier confidence patterns
        for reason, count in report["rejection_reasons"].items():
            if "below minimum" in reason and count >= 5:
                findings.append(
                    f"💡 {count}× rejections for: \"{reason[:90]}...\". Consider lowering "
                    f"the tier threshold if you want more signal flow (currently you're "
                    f"approving 0 and rejecting near-misses)."
                )
                break

    if report["approved_count"] > 0:
        # Check for oversized risk
        oversized = [t for t in report["approved_trades"]
                     if t.get("risk_dollar") and float(t["risk_dollar"]) > 1000]
        if oversized:
            biggest = max(oversized, key=lambda t: float(t["risk_dollar"]))
            findings.append(
                f"⚠️  {len(oversized)} approved trades exceed $1,000 notional risk "
                f"(biggest: {biggest['symbol']} @ ${float(biggest['risk_dollar']):,.0f}). "
                f"Account is $16K — these are leveraged options or unenforced caps. "
                f"Verify `dynamic_risk.py` is hard-capping at $320/trade."
            )

    # Silence detection across agents
    silent_agents = []
    for agent, last_iso in report["agent_last_seen"].items():
        last_d = _to_et_date_str(last_iso)
        days_silent = (_today_et().date() - datetime.strptime(last_d, "%Y-%m-%d").date()).days
        if days_silent >= 3:
            silent_agents.append((agent, days_silent))
    if silent_agents:
        names = ", ".join(f"{a} ({d}d)" for a, d in silent_agents[:5])
        findings.append(
            f"💤 Agents silent ≥3 days: {names}. May indicate broken data fetch or "
            f"a threshold permanently above their typical confidence range."
        )

    if not findings:
        findings.append("✅ No anomalies detected. Bot ran healthy ticks, "
                        "signals flowed through synthesis, no fetch error spikes.")

    return findings


# ── Report builder ───────────────────────────────────────────────────────────

class DailyReporter:

    def __init__(self):
        self.logger    = PerformanceLogger()
        self.evaluator = AgentEvaluator()

    def build_report(self) -> dict:
        now = _today_et()

        # --- Trade log (signals approved/rejected) ---
        signals_today = read_trade_log_today()
        approved = [s for s in signals_today if s.get("event") in APPROVED_EVENTS]
        rejected = [s for s in signals_today if s.get("event") in REJECTED_EVENTS]

        # --- Refresh ledger from scheduler.log + price-check open positions ---
        # Idempotent: parse_log just adds new PAPER TRADE entries it hasn't seen.
        # Errors here must NOT break the email — wrap defensively.
        ledger_status = {"available": _LEDGER_AVAILABLE, "added": 0, "total": 0,
                         "refresh": {}, "error": None}
        if _LEDGER_AVAILABLE:
            try:
                added, total = _ledger.parse_log()
                ledger_status["added"] = added
                ledger_status["total"] = total
            except Exception as e:
                ledger_status["error"] = f"parse_log failed: {e}"
            try:
                ledger_status["refresh"] = _ledger.refresh_open_positions()
            except Exception as e:
                ledger_status["error"] = (ledger_status["error"] or "") + f" | refresh failed: {e}"

        # --- Performance tracking: shadow (paper) + live (actual) ---
        # v2.2: shadow_pnl is now sourced from the structured ledger (data/paper_trades.csv).
        # Falls back to v2.1 trade_log.jsonl path if the ledger module isn't present.
        if _LEDGER_AVAILABLE:
            shadow_pnl = compute_paper_pnl_from_ledger()
        else:
            shadow_pnl = compute_shadow_pnl(approved)
        live_pnl   = read_live_fills()

        # --- v2.2: portfolio + per-agent attribution from ledger ---
        if _LEDGER_AVAILABLE:
            try:
                portfolio_summary = _ledger.cumulative_pnl()
                daily_series      = _ledger.daily_pnl_series()
                agent_attribution = _ledger.per_agent_attribution()
                open_pos_list     = _ledger.open_positions()
            except Exception as e:
                portfolio_summary = {"error": str(e)}
                daily_series      = []
                agent_attribution = []
                open_pos_list     = []
        else:
            portfolio_summary = {}
            daily_series      = []
            agent_attribution = []
            open_pos_list     = []

        # --- Scheduler log (truth source for tick health) ---
        sched = read_scheduler_today()

        # --- Approved trades structured for table ---
        approved_trades = []
        total_notional = 0.0
        for a in approved:
            risk = a.get("risk_dollar") or a.get("risk")
            try:
                risk_f = float(risk) if risk is not None else None
            except (TypeError, ValueError):
                risk_f = None
            if risk_f:
                total_notional += risk_f
            conf = a.get("confidence") or a.get("conf")
            try:
                conf_f = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf_f = None
            approved_trades.append({
                "time":        _to_et_time_str(a.get("timestamp", "")),
                "symbol":      a.get("symbol", "—"),
                "direction":   a.get("direction") or a.get("side") or "—",
                "agent":       a.get("agent", "—"),
                "confidence":  conf_f,
                "risk_dollar": risk_f,
            })

        # --- Rejection reason histogram ---
        rejection_reasons: dict[str, int] = {}
        for r in rejected:
            reason = r.get("reason", "unspecified")
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        # --- Per-agent activity ---
        agent_activity: dict[str, dict] = {}
        for s in signals_today:
            agent = s.get("agent", "Unknown")
            if agent not in agent_activity:
                agent_activity[agent] = {"approved": 0, "rejected": 0, "total": 0}
            agent_activity[agent]["total"] += 1
            if s.get("event") in APPROVED_EVENTS:
                agent_activity[agent]["approved"] += 1
            elif s.get("event") in REJECTED_EVENTS:
                agent_activity[agent]["rejected"] += 1

        # --- Closed trades / P&L (still from PerformanceLogger) ---
        try:
            recent = self.logger.get_trades(last_n_days=2)
        except Exception:
            recent = []
        closed_today = [t for t in recent
                        if _to_et_date_str(str(t.get("timestamp", ""))) == _today_str()]
        total_pnl  = sum(t.get("gross_pnl", 0) for t in closed_today)
        total_wins = sum(1 for t in closed_today if t.get("gross_pnl", 0) >= 0)

        # --- Last-seen per agent (silence detection) ---
        agent_last_seen = read_trade_log_recent_per_agent(days=14)

        # --- Raw signal totals from scheduler.log ---
        raw_signals_total = max((n for _, n in sched["raw_signal_ticks"]), default=0)
        passed_synthesis  = max((p for _, _, p in sched["synthesis_events"]), default=0)
        approved_batches_total = sum(n for _, n in sched["approved_batches"])

        # --- All-time totals ---
        try:
            all_trades = self.logger.get_trades(last_n_days=365)
        except Exception:
            all_trades = []
        all_time_pnl    = sum(t.get("gross_pnl", 0) for t in all_trades)
        all_time_trades = len(all_trades)

        account_balance  = float(os.getenv("ACCOUNT_BALANCE", "100000"))
        daily_return_pct = (total_pnl / account_balance * 100) if account_balance else 0

        report = {
            "date_display":         now.strftime("%A, %B %-d, %Y"),
            "generated_at":         now.strftime("%Y-%m-%d %H:%M ET"),
            "trading_mode":         TRADING_MODE,
            "sched":                sched,
            "approved_count":       len(approved),
            "rejected_count":       len(rejected),
            "approved_trades":      approved_trades,
            "total_notional":       round(total_notional, 2),
            "rejection_reasons":    rejection_reasons,
            "agent_activity":       agent_activity,
            "agent_last_seen":      agent_last_seen,
            "closed_trades":        len(closed_today),
            "total_pnl":            round(total_pnl, 2),
            "total_wins":           total_wins,
            "total_losses":         len(closed_today) - total_wins,
            "win_rate":             f"{total_wins/len(closed_today)*100:.0f}%" if closed_today else "—",
            "raw_signals_total":    raw_signals_total,
            "passed_synthesis":     passed_synthesis,
            "approved_batches":     approved_batches_total,
            "open_positions":       read_open_positions(),
            "all_time_pnl":         round(all_time_pnl, 2),
            "all_time_trades":      all_time_trades,
            "daily_return_pct":     round(daily_return_pct, 2),
            "account_balance":      account_balance,
            "shadow_pnl":           shadow_pnl,
            "live_pnl":             live_pnl,
            # v2.2 ledger-backed sections
            "ledger_status":        ledger_status,
            "portfolio_summary":    portfolio_summary,
            "daily_series":         daily_series,
            "agent_attribution":    agent_attribution,
            "open_positions_full":  [_ledger_position_to_dict(t) for t in open_pos_list] if _LEDGER_AVAILABLE else [],
        }
        report["findings"] = diagnose(report)
        return report

    # ── HTML ──────────────────────────────────────────────────────────────────

    def _format_pnl_column(self, label: str, summary: dict, is_active: bool,
                           color_active: str, color_dim: str) -> str:
        """Render one half of the Paper/Live performance panel."""
        bg     = "#eff6ff" if is_active else "#f1f5f9"
        border = color_active if is_active else "#cbd5e1"
        badge_text  = "● ACTIVE" if is_active else "○ STANDBY"
        badge_color = color_active if is_active else color_dim
        text_color  = "#0f172a" if is_active else "#94a3b8"

        pnl_val   = summary.get("total_pnl", 0.0)
        notional  = summary.get("total_notional", 0.0)
        wins      = summary.get("win_count", 0)
        losses    = summary.get("loss_count", 0)
        win_rate  = summary.get("win_rate", 0.0)
        positions = summary.get("positions", [])
        note      = summary.get("note", "")

        if is_active and positions:
            pnl_color = "#22c55e" if pnl_val >= 0 else "#ef4444"
            pnl_sign  = "+" if pnl_val >= 0 else ""
            pnl_str   = f"{pnl_sign}${pnl_val:,.2f}"
        elif is_active:
            pnl_color = "#94a3b8"
            pnl_str   = "$0.00"
        else:
            pnl_color = "#94a3b8"
            pnl_str   = "$0.00"

        sub_label = "Shadow P&L (simulated fills)" if "PAPER" in label else "Realized P&L (broker fills)"

        rows = (
            f'<tr><td style="padding:3px 0;color:{text_color}">Trades / signals:</td>'
            f'<td style="padding:3px 0;text-align:right;color:{text_color};font-weight:600">{len(positions)}</td></tr>'
            f'<tr><td style="padding:3px 0;color:{text_color}">Total notional:</td>'
            f'<td style="padding:3px 0;text-align:right;color:{text_color};font-weight:600">${notional:,.0f}</td></tr>'
            f'<tr><td style="padding:3px 0;color:{text_color}">Wins / Losses:</td>'
            f'<td style="padding:3px 0;text-align:right;color:{text_color};font-weight:600">{wins} / {losses}</td></tr>'
            f'<tr><td style="padding:3px 0;color:{text_color}">Win rate:</td>'
            f'<td style="padding:3px 0;text-align:right;color:{text_color};font-weight:600">{win_rate:.0f}%</td></tr>'
        )
        note_html = (
            f'<div style="font-size:10px;color:#94a3b8;margin-top:8px;font-style:italic;line-height:1.4">{note}</div>'
            if note else ""
        )

        return (
            f'<td style="width:48%;padding:14px;background:{bg};border:1px solid {border};'
            f'border-radius:8px;vertical-align:top">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
            f'<span style="font-size:11px;font-weight:700;color:{badge_color};letter-spacing:.05em">{label}</span>'
            f'<span style="font-size:10px;font-weight:700;color:{badge_color}">{badge_text}</span>'
            f'</div>'
            f'<div style="font-size:24px;font-weight:700;color:{pnl_color};margin:4px 0 2px">{pnl_str}</div>'
            f'<div style="font-size:11px;color:#64748b;margin-bottom:10px">{sub_label}</div>'
            f'<table style="width:100%;font-size:12px;border-collapse:collapse"><tbody>{rows}</tbody></table>'
            f'{note_html}'
            f'</td>'
        )

    def _format_position_table(self, positions: list[dict], top_n: int = 10) -> str:
        """Top winners + bottom losers table for the active mode."""
        if not positions:
            return '<p style="color:#94a3b8;font-style:italic;font-size:13px">No positions to display.</p>'

        winners = [p for p in positions if p["pnl"] >= 0][:top_n]
        losers  = [p for p in positions if p["pnl"] < 0][-top_n:]
        rows_to_show = winners + losers

        rows = ""
        for p in rows_to_show:
            pnl_color = "#22c55e" if p["pnl"] >= 0 else "#ef4444"
            pnl_sign  = "+" if p["pnl"] >= 0 else ""
            dir_color = "#22c55e" if p["direction"] in LONG_DIRECTIONS else "#ef4444"
            rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;font-size:11px;color:#64748b">{p["time"]}</td>'
                f'<td style="padding:5px 8px;font-weight:600">{p["symbol"]}</td>'
                f'<td style="padding:5px 8px;color:{dir_color};font-weight:600">{p["direction"]}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px">${p["entry"]:.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px">${p["current"]:.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-size:11px">${p["notional"]:,.0f}</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{pnl_color};font-weight:700">{pnl_sign}${p["pnl"]:,.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{pnl_color};font-size:11px">{pnl_sign}{p["pnl_pct"]:.1f}%</td>'
                f'</tr>'
            )
        return (
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="background:#1e293b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left">Time</th>'
            '<th style="padding:6px 8px;text-align:left">Symbol</th>'
            '<th style="padding:6px 8px;text-align:left">Side</th>'
            '<th style="padding:6px 8px;text-align:right">Entry</th>'
            '<th style="padding:6px 8px;text-align:right">Now</th>'
            '<th style="padding:6px 8px;text-align:right">Notional</th>'
            '<th style="padding:6px 8px;text-align:right">P&amp;L</th>'
            '<th style="padding:6px 8px;text-align:right">%</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
        )

    def _format_performance_panel(self, d: dict) -> str:
        """Two-column Paper/Live performance panel + position table for active side."""
        shadow = d["shadow_pnl"]
        live   = d["live_pnl"]
        is_paper_active = (d["trading_mode"] == "paper")

        paper_col = self._format_pnl_column(
            "PAPER (TEST)", shadow,
            is_active=is_paper_active,
            color_active="#1e40af", color_dim="#64748b",
        )
        live_col = self._format_pnl_column(
            "ACTUAL (LIVE)", live,
            is_active=not is_paper_active,
            color_active="#15803d", color_dim="#64748b",
        )

        active_summary = shadow if is_paper_active else live
        positions      = active_summary.get("positions", [])
        active_label   = "Paper / shadow" if is_paper_active else "Live / actual"

        # Biggest winner / loser strip
        bw = active_summary.get("biggest_winner")
        bl = active_summary.get("biggest_loser")
        ext_html = ""
        if bw or bl:
            cells = []
            if bw and bw["pnl"] > 0:
                cells.append(
                    f'<div style="flex:1;padding:8px 12px;background:#f0fdf4;border-radius:6px;margin-right:6px">'
                    f'<div style="font-size:10px;color:#15803d;font-weight:700">🏆 BEST</div>'
                    f'<div style="font-size:13px;margin-top:2px"><strong>{bw["symbol"]}</strong> {bw["direction"]} '
                    f'<span style="color:#22c55e;font-weight:700">+${bw["pnl"]:,.2f}</span> '
                    f'<span style="color:#64748b;font-size:11px">({bw["pnl_pct"]:+.1f}%)</span></div></div>'
                )
            if bl and bl["pnl"] < 0:
                cells.append(
                    f'<div style="flex:1;padding:8px 12px;background:#fef2f2;border-radius:6px">'
                    f'<div style="font-size:10px;color:#991b1b;font-weight:700">📉 WORST</div>'
                    f'<div style="font-size:13px;margin-top:2px"><strong>{bl["symbol"]}</strong> {bl["direction"]} '
                    f'<span style="color:#ef4444;font-weight:700">${bl["pnl"]:,.2f}</span> '
                    f'<span style="color:#64748b;font-size:11px">({bl["pnl_pct"]:+.1f}%)</span></div></div>'
                )
            if cells:
                ext_html = (
                    '<div style="display:flex;gap:6px;margin-top:10px">' + "".join(cells) + '</div>'
                )

        # Per-agent shadow P&L mini-table
        agent_html = ""
        by_agent = active_summary.get("by_agent", {})
        if by_agent:
            agent_rows = ""
            for agent, stats in sorted(by_agent.items(), key=lambda x: -x[1]["pnl"]):
                clr  = "#22c55e" if stats["pnl"] >= 0 else "#ef4444"
                sign = "+" if stats["pnl"] >= 0 else ""
                agent_rows += (
                    f'<tr><td style="padding:4px 8px;font-size:12px">{agent}</td>'
                    f'<td style="padding:4px 8px;text-align:right;font-size:12px">{stats["count"]}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:{clr};font-weight:700">{sign}${stats["pnl"]:,.2f}</td></tr>'
                )
            agent_html = (
                f'<div style="font-size:12px;font-weight:700;color:#475569;margin:14px 0 4px">'
                f'{active_label} P&amp;L by agent</div>'
                f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
                f'<thead><tr style="background:#f1f5f9">'
                f'<th style="padding:5px 8px;text-align:left">Agent</th>'
                f'<th style="padding:5px 8px;text-align:right">Trades</th>'
                f'<th style="padding:5px 8px;text-align:right">P&amp;L</th>'
                f'</tr></thead><tbody>' + agent_rows + '</tbody></table>'
            )

        positions_html = self._format_position_table(positions, top_n=10)

        caveat = (
            '<div style="font-size:10px;color:#94a3b8;margin-top:10px;font-style:italic;line-height:1.5">'
            '<strong>Shadow P&amp;L caveats:</strong> assumes perfect fills at signal-time price '
            '(no slippage), assumes positions still held at last close (no stops or profit targets), '
            'and treats every signal as stock-equivalent (option leverage NOT modeled). '
            'Real fills will differ. The LIVE column will replace this column once Phase B is wired.'
            '</div>'
        ) if is_paper_active else ""

        return (
            '<table style="width:100%;border-collapse:collapse;margin:10px 0">'
            '<tr>' + paper_col + '<td style="width:8px"></td>' + live_col + '</tr>'
            '</table>'
            + ext_html
            + agent_html
            + f'<div style="font-size:12px;font-weight:700;color:#475569;margin:14px 0 4px">'
              f'{active_label} positions — top winners &amp; losers</div>'
            + positions_html
            + caveat
        )

    # ── v2.2 ledger-backed sections ──────────────────────────────────────────

    def _format_daily_trends_section(self, d: dict) -> str:
        """Today vs. yesterday vs. trailing 5-day average."""
        series = d.get("daily_series") or []
        today = _today_str()

        today_row = next((r for r in series if r["date"] == today), None)
        prior_rows = [r for r in series if r["date"] < today]
        prior_rows.sort(key=lambda r: r["date"])
        yest_row  = prior_rows[-1] if prior_rows else None
        last5     = prior_rows[-5:]
        avg5      = (sum(r["total"] for r in last5) / len(last5)) if last5 else 0.0

        def card(label, value, color=None):
            if color is None:
                color = "#22c55e" if isinstance(value, str) and value.startswith("+") \
                        else ("#ef4444" if isinstance(value, str) and value.startswith("-") else "#1e293b")
            return (f'<div class="kpi"><div class="val" style="color:{color}">{value}</div>'
                    f'<div class="lbl">{label}</div></div>')

        def fmt_pnl(val):
            if val is None: return "—"
            sign = "+" if val >= 0 else ""
            return f"{sign}${val:,.2f}"

        today_pnl = today_row["total"] if today_row else 0.0
        today_count = today_row["count"] if today_row else 0
        yest_pnl  = yest_row["total"] if yest_row else None
        diff_vs_avg = today_pnl - avg5 if last5 else None

        cards_html = (
            '<div class="kpi-row">'
            + card("Today's P&amp;L",  fmt_pnl(today_pnl))
            + card("Today's Trades",  str(today_count), color="#1e293b")
            + card("Yesterday",       fmt_pnl(yest_pnl))
            + card("5-day Avg",       fmt_pnl(avg5) if last5 else "—")
            + card("vs. 5d Avg",      fmt_pnl(diff_vs_avg) if diff_vs_avg is not None else "—")
            + '</div>'
        )

        # Last-7-day daily P&L mini-table
        last7 = (prior_rows + ([today_row] if today_row else []))[-7:]
        if last7:
            rows = ""
            for r in last7:
                clr  = "#22c55e" if r["total"] >= 0 else "#ef4444"
                sign = "+" if r["total"] >= 0 else ""
                rows += (
                    f'<tr><td style="padding:5px 8px;font-family:monospace;font-size:12px">{r["date"]}</td>'
                    f'<td style="padding:5px 8px;text-align:right">{r["count"]}</td>'
                    f'<td style="padding:5px 8px;text-align:right">{r["wins"]}/{r["losses"]}</td>'
                    f'<td style="padding:5px 8px;text-align:right;color:{clr};font-weight:700">{sign}${r["total"]:,.2f}</td>'
                    f'<td style="padding:5px 8px;text-align:right;color:#64748b;font-size:11px">'
                    f'realized ${r["realized"]:+,.2f} • unrlz ${r["unrealized"]:+,.2f}</td></tr>'
                )
            table_html = (
                '<table style="width:100%;border-collapse:collapse;font-size:13px">'
                '<thead><tr style="background:#1e293b;color:#fff">'
                '<th style="padding:6px 8px;text-align:left">Date (ET)</th>'
                '<th style="padding:6px 8px;text-align:right">Trades</th>'
                '<th style="padding:6px 8px;text-align:right">W/L</th>'
                '<th style="padding:6px 8px;text-align:right">Day P&amp;L</th>'
                '<th style="padding:6px 8px;text-align:right">Breakdown</th>'
                '</tr></thead><tbody>' + rows + '</tbody></table>'
            )
        else:
            table_html = '<p style="color:#94a3b8;font-style:italic">No daily history yet — ledger is empty.</p>'

        return cards_html + '<div style="margin-top:14px">' + table_html + '</div>'

    def _format_portfolio_section(self, d: dict) -> str:
        """Cumulative since inception of the paper-trading ledger."""
        p = d.get("portfolio_summary") or {}
        if not p or p.get("error"):
            return f'<p style="color:#94a3b8;font-style:italic">Portfolio data unavailable. {p.get("error", "")}</p>'

        if p.get("trade_count", 0) == 0:
            return '<p style="color:#94a3b8;font-style:italic">No paper trades in ledger yet. Run <code>python3 trade_ledger.py</code> on the VM to backfill from scheduler.log.</p>'

        total = p["total_pnl"]
        clr   = "#22c55e" if total >= 0 else "#ef4444"
        sign  = "+" if total >= 0 else ""

        def kpi(label, value, color="#1e293b"):
            return (f'<div class="kpi"><div class="val" style="color:{color}">{value}</div>'
                    f'<div class="lbl">{label}</div></div>')

        def signed(v):
            s = "+" if v >= 0 else "-"
            return f"{s}${abs(v):,.2f}"

        kpi_row = (
            '<div class="kpi-row">'
            + kpi("Total P&amp;L (since start)", signed(total), color=clr)
            + kpi("Realized",   signed(p['realized_pnl']),   color="#1e293b")
            + kpi("Unrealized", signed(p['unrealized_pnl']), color="#64748b")
            + kpi("Trades",     f"{p['trade_count']}",       color="#1e293b")
            + kpi("Win rate",   f"{p['win_rate']:.1f}%",     color="#1e293b")
            + '</div>'
        )

        best  = p.get("best_day")
        worst = p.get("worst_day")

        meta_rows = (
            f'<tr><td style="padding:5px 8px;color:#64748b">First trade</td>'
            f'<td style="padding:5px 8px;text-align:right;font-family:monospace">{p["first_trade_date"]}</td></tr>'
            f'<tr><td style="padding:5px 8px;color:#64748b">Last trade</td>'
            f'<td style="padding:5px 8px;text-align:right;font-family:monospace">{p["last_trade_date"]}</td></tr>'
            f'<tr><td style="padding:5px 8px;color:#64748b">Trading days w/ activity</td>'
            f'<td style="padding:5px 8px;text-align:right;font-weight:600">{p["trading_days"]}</td></tr>'
            f'<tr><td style="padding:5px 8px;color:#64748b">Open positions</td>'
            f'<td style="padding:5px 8px;text-align:right;font-weight:600">{p["open_count"]}</td></tr>'
            f'<tr><td style="padding:5px 8px;color:#64748b">Closed positions</td>'
            f'<td style="padding:5px 8px;text-align:right;font-weight:600">{p["closed_count"]} ({p["wins"]}W / {p["losses"]}L)</td></tr>'
        )
        if best:
            meta_rows += (
                f'<tr><td style="padding:5px 8px;color:#15803d">🏆 Best day</td>'
                f'<td style="padding:5px 8px;text-align:right;color:#22c55e;font-weight:700">'
                f'+${best["total"]:,.2f} on {best["date"]}</td></tr>'
            )
        if worst:
            meta_rows += (
                f'<tr><td style="padding:5px 8px;color:#991b1b">📉 Worst day</td>'
                f'<td style="padding:5px 8px;text-align:right;color:#ef4444;font-weight:700">'
                f'${worst["total"]:,.2f} on {worst["date"]}</td></tr>'
            )

        meta_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px">'
            '<tbody>' + meta_rows + '</tbody></table>'
        )

        return kpi_row + meta_html

    def _format_agent_evaluator_section(self, d: dict) -> str:
        """Per-agent attribution table: every agent that contributed to any trade."""
        agents = d.get("agent_attribution") or []
        if not agents:
            return '<p style="color:#94a3b8;font-style:italic">No agent attribution data yet.</p>'

        rows = ""
        for a in agents:
            clr  = "#22c55e" if a["total_pnl"] >= 0 else "#ef4444"
            sign = "+" if a["total_pnl"] >= 0 else ""
            wr   = a["win_rate"]
            wr_color = "#22c55e" if wr >= 50 else ("#f59e0b" if wr >= 33 else "#ef4444")
            best  = a["best_trade"]
            worst = a["worst_trade"]
            best_str  = f'{best["symbol"]} {best["side"]} ${best["pnl"]:+,.0f}'   if best  else "—"
            worst_str = f'{worst["symbol"]} {worst["side"]} ${worst["pnl"]:+,.0f}' if worst else "—"
            rows += (
                f'<tr>'
                f'<td style="padding:6px 8px;font-weight:600;font-size:12px">{a["agent"]}</td>'
                f'<td style="padding:6px 8px;text-align:right">{a["trades_total"]} '
                f'<span style="color:#64748b;font-size:10px">({a["as_primary"]}p/{a["as_contributor"]}c)</span></td>'
                f'<td style="padding:6px 8px;text-align:right">{a["trades_open"]}/{a["trades_closed"]}</td>'
                f'<td style="padding:6px 8px;text-align:right;color:{wr_color};font-weight:700">{wr:.0f}%</td>'
                f'<td style="padding:6px 8px;text-align:right">${a["avg_pnl"]:+,.2f}</td>'
                f'<td style="padding:6px 8px;text-align:right;color:{clr};font-weight:700">{sign}${a["total_pnl"]:,.2f}</td>'
                f'<td style="padding:6px 8px;font-size:10px;color:#15803d">{best_str}</td>'
                f'<td style="padding:6px 8px;font-size:10px;color:#991b1b">{worst_str}</td>'
                f'</tr>'
            )

        return (
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="background:#1e293b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left">Agent</th>'
            '<th style="padding:6px 8px;text-align:right">Trades</th>'
            '<th style="padding:6px 8px;text-align:right">Open/Closed</th>'
            '<th style="padding:6px 8px;text-align:right">Win %</th>'
            '<th style="padding:6px 8px;text-align:right">Avg P&amp;L</th>'
            '<th style="padding:6px 8px;text-align:right">Total P&amp;L</th>'
            '<th style="padding:6px 8px;text-align:left">Best</th>'
            '<th style="padding:6px 8px;text-align:left">Worst</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
            + '<p style="font-size:10px;color:#94a3b8;margin:6px 0 0;font-style:italic">'
              'Each trade attributes to its primary agent + every contributor — '
              '"4 (1p/3c)" means 4 total: 1 as primary, 3 as contributor in MetaAgent decisions.</p>'
        )

    def _format_open_positions_section(self, d: dict) -> str:
        """Currently-open positions with target/stop and unrealized P&L."""
        opens = d.get("open_positions_full") or []
        if not opens:
            return '<p style="color:#94a3b8;font-style:italic">No open positions.</p>'

        rows = ""
        for p in sorted(opens, key=lambda x: -x["pnl"]):
            clr  = "#22c55e" if p["pnl"] >= 0 else "#ef4444"
            sign = "+" if p["pnl"] >= 0 else ""
            side_color = "#22c55e" if p["side"] == "LONG" else "#ef4444"
            cur = f"${p['current']:.2f}" if p["current"] else "—"
            rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;font-family:monospace;font-size:11px">{p["opened_at_et"][5:16]}</td>'
                f'<td style="padding:5px 8px;font-weight:600">{p["symbol"]}</td>'
                f'<td style="padding:5px 8px;color:{side_color};font-weight:600">{p["side"]}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px">${p["entry"]:.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px">{cur}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px;color:#15803d">${p["target"]:.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;font-family:monospace;font-size:11px;color:#991b1b">${p["stop"]:.2f}</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{clr};font-weight:700">{sign}${p["pnl"]:,.2f}</td>'
                f'<td style="padding:5px 8px;font-size:10px;color:#64748b">{p["primary_agent"]}</td>'
                f'</tr>'
            )

        return (
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="background:#1e293b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left">Opened</th>'
            '<th style="padding:6px 8px;text-align:left">Symbol</th>'
            '<th style="padding:6px 8px;text-align:left">Side</th>'
            '<th style="padding:6px 8px;text-align:right">Entry</th>'
            '<th style="padding:6px 8px;text-align:right">Now</th>'
            '<th style="padding:6px 8px;text-align:right">Target</th>'
            '<th style="padding:6px 8px;text-align:right">Stop</th>'
            '<th style="padding:6px 8px;text-align:right">Unrealized</th>'
            '<th style="padding:6px 8px;text-align:left">Agent</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
        )

    def format_email_html(self, d: dict) -> str:
        sched = d["sched"]
        pnl_color = "#22c55e" if d["total_pnl"] >= 0 else "#ef4444"
        pnl_sign  = "+" if d["total_pnl"] >= 0 else ""

        # ─── Performance Tracking — Paper (shadow) vs. Live (actual) ──────────
        perf_html = self._format_performance_panel(d)

        # ─── v2.2 ledger-backed sections ──────────────────────────────────────
        daily_trends_html  = self._format_daily_trends_section(d)
        portfolio_html     = self._format_portfolio_section(d)
        agent_eval_html    = self._format_agent_evaluator_section(d)
        open_positions_html = self._format_open_positions_section(d)

        # Ledger status banner — surfaces parsing/refresh problems immediately
        ls = d.get("ledger_status") or {}
        if not ls.get("available"):
            ledger_banner = (
                '<div style="background:#fef2f2;padding:10px 14px;border-radius:6px;'
                'margin-bottom:6px;font-size:13px;border-left:3px solid #ef4444">'
                '⚠️  trade_ledger module not loaded on the VM. '
                'Upload <code>trade_ledger.py</code> next to <code>daily_reporter.py</code> '
                'to enable Daily Trends, Portfolio, and Agent Evaluator sections.'
                '</div>'
            )
        elif ls.get("error"):
            ledger_banner = (
                f'<div style="background:#fffbeb;padding:10px 14px;border-radius:6px;'
                f'margin-bottom:6px;font-size:13px;border-left:3px solid #f59e0b">'
                f'⚠️  Ledger refresh had a partial error: <code>{ls["error"][:200]}</code>'
                f'</div>'
            )
        else:
            r = ls.get("refresh") or {}
            ledger_banner = (
                f'<div style="background:#f0fdf4;padding:8px 14px;border-radius:6px;'
                f'margin-bottom:6px;font-size:12px;color:#15803d">'
                f'📒 Ledger: {ls["total"]} trades total (+{ls["added"]} new this run). '
                f'Open-position refresh: {r.get("checked", 0)} checked, '
                f'{r.get("closed_target", 0)} hit target, '
                f'{r.get("closed_stop", 0)} hit stop, '
                f'{r.get("expired", 0)} expired, '
                f'{r.get("still_open", 0)} still open.'
                f'</div>'
            )

        # Findings panel (top of email — most important)
        findings_html = ""
        for f in d["findings"]:
            bg = "#fef2f2" if f.startswith("⚠️") else ("#fffbeb" if f.startswith("💡") else
                  "#f0fdf4" if f.startswith("✅") else "#f1f5f9")
            findings_html += f'<div style="background:{bg};padding:10px 14px;border-radius:6px;margin-bottom:6px;font-size:13px">{f}</div>'

        # Approved trades table
        if d["approved_trades"]:
            rows = ""
            for t in d["approved_trades"][:50]:
                conf_s = f"{t['confidence']:.2f}" if t["confidence"] is not None else "—"
                risk_s = f"${t['risk_dollar']:,.0f}" if t["risk_dollar"] is not None else "—"
                dir_color = "#22c55e" if str(t["direction"]).lower() == "long" else "#ef4444"
                rows += (f'<tr><td style="padding:6px 10px;font-size:12px">{t["time"]}</td>'
                         f'<td style="padding:6px 10px;font-weight:600">{t["symbol"]}</td>'
                         f'<td style="padding:6px 10px;color:{dir_color};font-weight:600">{t["direction"]}</td>'
                         f'<td style="padding:6px 10px;text-align:right">{conf_s}</td>'
                         f'<td style="padding:6px 10px;text-align:right">{risk_s}</td>'
                         f'<td style="padding:6px 10px;font-size:11px;color:#64748b">{t["agent"]}</td></tr>')
            approved_html = f'<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#1e293b;color:#fff"><th style="padding:8px 10px;text-align:left">Time</th><th style="padding:8px 10px;text-align:left">Symbol</th><th style="padding:8px 10px;text-align:left">Side</th><th style="padding:8px 10px;text-align:right">Conf</th><th style="padding:8px 10px;text-align:right">Risk $</th><th style="padding:8px 10px;text-align:left">Agent</th></tr></thead><tbody>{rows}</tbody></table>'
            if len(d["approved_trades"]) > 50:
                approved_html += f'<p style="font-size:12px;color:#64748b;margin:6px 0 0">Showing first 50 of {len(d["approved_trades"])} approved trades.</p>'
        else:
            approved_html = '<p style="color:#94a3b8;font-style:italic">No approved trades today.</p>'

        # Rejection reasons
        if d["rejection_reasons"]:
            rrows = ""
            for reason, count in sorted(d["rejection_reasons"].items(), key=lambda x: -x[1])[:10]:
                rrows += (f'<tr><td style="padding:6px 10px;font-size:12px">{reason[:120]}</td>'
                          f'<td style="padding:6px 10px;text-align:right;font-weight:700">{count}</td></tr>')
            reject_html = f'<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#1e293b;color:#fff"><th style="padding:8px 10px;text-align:left">Reason</th><th style="padding:8px 10px;text-align:right">Count</th></tr></thead><tbody>{rrows}</tbody></table>'
        else:
            reject_html = '<p style="color:#94a3b8;font-style:italic">No rejections today.</p>'

        # Per-agent activity
        if d["agent_activity"]:
            arows = ""
            for agent, stats in sorted(d["agent_activity"].items(), key=lambda x: -x[1]["total"])[:25]:
                arows += (f'<tr><td style="padding:6px 10px;font-size:12px;font-weight:600">{agent[:60]}</td>'
                          f'<td style="padding:6px 10px;text-align:right;color:#22c55e">{stats["approved"]}</td>'
                          f'<td style="padding:6px 10px;text-align:right;color:#ef4444">{stats["rejected"]}</td>'
                          f'<td style="padding:6px 10px;text-align:right">{stats["total"]}</td></tr>')
            agents_html = f'<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#1e293b;color:#fff"><th style="padding:8px 10px;text-align:left">Agent</th><th style="padding:8px 10px;text-align:right">✓</th><th style="padding:8px 10px;text-align:right">✗</th><th style="padding:8px 10px;text-align:right">Total</th></tr></thead><tbody>{arows}</tbody></table>'
        else:
            agents_html = '<p style="color:#94a3b8;font-style:italic">No agent activity recorded today.</p>'

        # Fetch errors
        if sched["fetch_404_symbols"]:
            err_items = sorted(sched["fetch_404_symbols"].items(), key=lambda x: -x[1])[:15]
            err_html = '<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:#1e293b;color:#fff"><th style="padding:8px 10px;text-align:left">Symbol</th><th style="padding:8px 10px;text-align:right">404 count</th></tr></thead><tbody>'
            for sym, cnt in err_items:
                err_html += f'<tr><td style="padding:6px 10px">{sym}</td><td style="padding:6px 10px;text-align:right;font-weight:700">{cnt}</td></tr>'
            err_html += '</tbody></table>'
            err_html += f'<p style="font-size:12px;color:#64748b;margin:6px 0 0">Total errors today: {sched["error_count"]:,}</p>'
        else:
            err_html = '<p style="color:#94a3b8;font-style:italic">No fetch errors today.</p>'

        # Tick health
        tick_pct = (sched["tick_count"] / 390 * 100) if sched["tick_count"] else 0
        tick_color = "#22c55e" if tick_pct >= 80 else ("#f59e0b" if tick_pct >= 30 else "#ef4444")
        tick_html = (
            f'<table style="width:100%;border-collapse:collapse;font-size:13px"><tbody>'
            f'<tr><td style="padding:6px 10px;font-weight:600">Ticks observed</td>'
            f'<td style="padding:6px 10px;text-align:right;color:{tick_color};font-weight:700">{sched["tick_count"]:,} / ~390 ({tick_pct:.0f}%)</td></tr>'
            f'<tr><td style="padding:6px 10px;font-weight:600">First log entry</td>'
            f'<td style="padding:6px 10px;text-align:right;font-family:monospace;font-size:12px">{sched["first_log"] or "—"}</td></tr>'
            f'<tr><td style="padding:6px 10px;font-weight:600">Last log entry</td>'
            f'<td style="padding:6px 10px;text-align:right;font-family:monospace;font-size:12px">{sched["last_log"] or "—"}</td></tr>'
            f'<tr><td style="padding:6px 10px;font-weight:600">Raw signals (peak tick)</td>'
            f'<td style="padding:6px 10px;text-align:right;font-weight:700">{d["raw_signals_total"]}</td></tr>'
            f'<tr><td style="padding:6px 10px;font-weight:600">Passed synthesis (peak)</td>'
            f'<td style="padding:6px 10px;text-align:right;font-weight:700">{d["passed_synthesis"]}</td></tr>'
            f'</tbody></table>'
        )

        # KPI cards
        signals_card = f"{d['approved_count']}✓ / {d['rejected_count']}✗"
        notional_card = f"${d['total_notional']:,.0f}"

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f8fafc; margin:0; padding:0; color:#1e293b; }}
  .wrapper {{ max-width:680px; margin:32px auto; background:#fff;
              border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }}
  .header {{ background:#0f172a; padding:28px 32px; }}
  .header h1 {{ color:#fff; margin:0; font-size:20px; font-weight:700; }}
  .header p  {{ color:#94a3b8; margin:4px 0 0; font-size:13px; }}
  .body {{ padding:24px 32px; }}
  .kpi-row {{ display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }}
  .kpi {{ flex:1; min-width:110px; background:#f1f5f9; border-radius:8px;
         padding:14px; text-align:center; }}
  .kpi .val {{ font-size:20px; font-weight:700; }}
  .kpi .lbl {{ font-size:11px; color:#64748b; margin-top:3px; }}
  .section-title {{ font-size:14px; font-weight:700; color:#1e293b;
                    margin:24px 0 8px; text-transform:uppercase;
                    letter-spacing:0.04em; }}
  .footer {{ background:#f1f5f9; padding:14px 32px; font-size:11px;
             color:#94a3b8; text-align:center; }}
</style></head><body>
<div class="wrapper">
  <div class="header">
    <h1>📊 Daily Trading Report — v2.2</h1>
    <p>{d['date_display']}  •  Generated {d['generated_at']}  •  Mode: <strong style="color:#fff">{d['trading_mode'].upper()}</strong></p>
  </div>
  <div class="body">

    <div class="section-title">🔍 What I Noticed Today</div>
    {findings_html}

    <div class="section-title">💰 Performance Tracking — Paper vs. Live</div>
    {perf_html}

    {ledger_banner}

    <div class="section-title">📈 Daily Trends</div>
    {daily_trends_html}

    <div class="section-title">🧮 Portfolio Since Inception</div>
    {portfolio_html}

    <div class="section-title">📂 Currently Open Positions</div>
    {open_positions_html}

    <div class="section-title">🤖 Per-Agent Evaluator</div>
    {agent_eval_html}

    <div class="kpi-row" style="margin-top:20px">
      <div class="kpi">
        <div class="val" style="color:{pnl_color}">{pnl_sign}${d['total_pnl']:,.2f}</div>
        <div class="lbl">Closed P&amp;L</div>
      </div>
      <div class="kpi">
        <div class="val">{signals_card}</div>
        <div class="lbl">Signals (✓/✗)</div>
      </div>
      <div class="kpi">
        <div class="val">{notional_card}</div>
        <div class="lbl">Notional Approved</div>
      </div>
      <div class="kpi">
        <div class="val">{d['closed_trades']}</div>
        <div class="lbl">Closed Trades</div>
      </div>
      <div class="kpi">
        <div class="val">{d['raw_signals_total']}</div>
        <div class="lbl">Raw Signals Peak</div>
      </div>
    </div>

    <div class="section-title">✅ Today's Approved Trades</div>
    {approved_html}

    <div class="section-title">🚫 Top Rejection Reasons</div>
    {reject_html}

    <div class="section-title">🤖 Agent Activity</div>
    {agents_html}

    <div class="section-title">📡 Fetch Errors (yfinance 404s)</div>
    {err_html}

    <div class="section-title">⏱  Tick Health</div>
    {tick_html}

    <div class="section-title">🧭 Running Totals</div>
    <p style="font-size:13px;color:#475569;margin:0">
      All-time paper P&amp;L: <strong>${d['all_time_pnl']:+,.2f}</strong>
      across {d['all_time_trades']} closed trades.
    </p>
  </div>
  <div class="footer">
    Trading Bot Daily Report v2.2 • Paper Trading (Phase D) • Auto-generated.<br>
    Truth sources: data/paper_trades.csv (ledger) + scheduler.log + yfinance.<br>
    Live column auto-activates when logs/live_fills.jsonl appears (Phase B).
  </div>
</div></body></html>"""

    # ── Send ──────────────────────────────────────────────────────────────────

    def send(self, html: str, subject: str | None = None) -> bool:
        if not GMAIL_ADDRESS or not GMAIL_APP_PW:
            print("ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
            return False
        now = _today_et()
        subject = subject or f"Trading Bot — Daily Report v2 ({now.strftime('%b %-d, %Y')})"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = REPORT_TO_EMAIL
        msg.attach(MIMEText(html, "html"))
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
                server.sendmail(GMAIL_ADDRESS, REPORT_TO_EMAIL, msg.as_string())
            print(f"✅  Daily report v2 sent to {REPORT_TO_EMAIL}")
            return True
        except Exception as e:
            print(f"❌  Failed to send email: {e}")
            return False

    def save_html(self, html: str) -> str:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fn = LOGS_DIR / f"daily_report_{_today_et().strftime('%Y-%m-%d')}.html"
        fn.write_text(html)
        print(f"Report saved → {fn}")
        return str(fn)


# ── Entry ─────────────────────────────────────────────────────────────────────

def _crash_log(exc: BaseException) -> None:
    """Write any exception to logs/daily_reporter_crash.log so cron failures
    don't disappear into the void."""
    import traceback
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    crash = LOGS_DIR / "daily_reporter_crash.log"
    with crash.open("a") as f:
        f.write(f"\n\n=== CRASH @ {datetime.now(ET).isoformat()} ===\n")
        f.write(f"GMAIL_ADDRESS set: {bool(GMAIL_ADDRESS)}\n")
        f.write(f"GMAIL_APP_PASSWORD set: {bool(GMAIL_APP_PW)}\n")
        f.write(f"BASE_DIR: {BASE_DIR}\n")
        f.write(f".env exists: {(BASE_DIR / '.env').exists()}\n")
        f.write(traceback.format_exc())


if __name__ == "__main__":
    try:
        reporter = DailyReporter()
        data     = reporter.build_report()
        html     = reporter.format_email_html(data)
        reporter.save_html(html)
        if "--send-now" in sys.argv:
            ok = reporter.send(html)
            if not ok:
                # Send returned False — credential or SMTP problem. Log it.
                _crash_log(RuntimeError(
                    f"reporter.send() returned False. "
                    f"GMAIL_ADDRESS empty: {not GMAIL_ADDRESS}. "
                    f"GMAIL_APP_PW empty: {not GMAIL_APP_PW}. "
                    f".env path checked: {BASE_DIR / '.env'}"
                ))
                sys.exit(1)
        else:
            print("Report generated (HTML saved). Pass --send-now to email it.")
    except Exception as e:
        _crash_log(e)
        print(f"❌  Reporter crashed: {e}", file=sys.stderr)
        sys.exit(1)
