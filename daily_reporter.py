"""
daily_reporter.py — one market-day PAPER scorecard (not the old v2 dump).

Sends only on NYSE regular-session days (Mon–Fri, skip US market holidays).
Weekends/holidays: exit 0 with a skip log. No Slack copy of this email.

Kill switches (VM .env):
  ENABLE_DAILY_EMAIL=false     silence this email entirely (default: true)
  ENABLE_SLACK_SUMMARY=false   daily Slack dump stays OFF (default: false)

Cron (single sender — remove send_recap_email.py ~16:30 if still present):
    35 16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/daily_reporter.py --send-now
The 1-5 crontab still fires on NYSE holidays; this script no-ops those days.
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

# Kill switches. ENABLE_DAILY_EMAIL defaults ON because this file *is*
# the replacement scorecard. Set false on the VM to silence email.
# ENABLE_SLACK_SUMMARY defaults OFF — no duplicate trading dump to Slack.
def _env_on(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLE_DAILY_EMAIL = _env_on("ENABLE_DAILY_EMAIL", "true")
ENABLE_SLACK_SUMMARY = _env_on("ENABLE_SLACK_SUMMARY", "false")


def daily_email_enabled() -> bool:
    return _env_on("ENABLE_DAILY_EMAIL", "true")


def is_open_market_report_day(now: datetime | None = None) -> bool:
    """True only on US equity RTH session days (Mon–Fri, not NYSE holidays)."""
    try:
        from session_gates import is_nyse_session_day
        return is_nyse_session_day(now)
    except Exception:
        now = now or datetime.now(ET)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ET)
        return now.weekday() < 5


def skip_send_reason(*, send_now: bool) -> str | None:
    """Why --send-now should no-op. None means sending is allowed."""
    if not send_now:
        return None
    if not daily_email_enabled():
        return "ENABLE_DAILY_EMAIL=false — daily email kill switch; not sending"
    if not is_open_market_report_day():
        return "skip — market closed (weekend or NYSE holiday)"
    return None


def format_email_subject(snap: dict | None = None, date: str | None = None) -> str:
    """[PAPER] Market day — YYYY-MM-DD — equity $X (day ±Y%)"""
    snap = snap or {}
    date = date or snap.get("today") or _today_et().strftime("%Y-%m-%d")
    eq = snap.get("equity")
    prev = snap.get("prev_close")
    if eq is None:
        return f"[PAPER] Market day — {date} — equity n/a"
    day_pct = ((float(eq) / float(prev)) - 1) * 100 if prev else 0.0
    sign = "+" if day_pct >= 0 else ""
    return f"[PAPER] Market day — {date} — equity ${float(eq):,.0f} (day {sign}{day_pct:.2f}%)"


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _rotations_today(today: str) -> list[dict]:
    path = LOGS_DIR / "rotation_log.jsonl"
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ts = str(rec.get("timestamp") or "")
            if today in ts:
                rows.append(rec)
    except Exception:
        return rows
    return rows


def _load_meta_weights() -> dict:
    """Read-only copy of MetaAgent weights for the scorecard. Never writes."""
    try:
        from meta_agent import MetaAgent
        return dict(MetaAgent._load_performance_weights() or {})
    except Exception:
        try:
            from meta_agent import DEFAULT_WEIGHTS
            return dict(DEFAULT_WEIGHTS)
        except Exception:
            return {}


def _leaf_agent_names(raw: str) -> list[str]:
    """Unwrap MetaAgent(...) into leaf names. Empty for bare MetaAgent."""
    if _LEDGER_AVAILABLE:
        return list(_ledger.expand_agent_names(raw))
    raw = (raw or "").strip()
    if not raw or raw in {"MetaAgent", "BrokerSync"}:
        return []
    m = re.match(r"^([A-Za-z_]+)\s*\(([^)]*)\)\s*$", raw)
    if m and m.group(1).strip() == "MetaAgent":
        return [p.strip() for p in m.group(2).split(",")
                if p.strip() and p.strip() not in {"MetaAgent", "BrokerSync"}]
    if raw.startswith("MetaAgent("):
        return []
    return [raw]


def _is_wrapper_agent_name(name: str) -> bool:
    if _LEDGER_AVAILABLE:
        return bool(_ledger.is_wrapper_agent_name(name))
    n = (name or "").strip()
    return (not n) or n in {"MetaAgent", "BrokerSync"} or n.startswith("MetaAgent(")


def scorecard_agent_roster(d: dict | None = None) -> list[dict]:
    """Leaf-agent roster: status, MetaAgent weight, P&L.

    MetaAgent(*) wrapper labels are stripped. Soft leaf weights are the real
    MetaAgent values. Unknown wrapper names are never invented at 1.00 —
    that was polluting kill/BENCH reads on the Learning Loop scorecard.

    Tests (and callers) may pass ``d['agent_roster']`` to skip disk/ledger.
    """
    d = d or {}
    if "agent_roster" in d:
        return [
            row for row in (d.get("agent_roster") or [])
            if isinstance(row, dict)
            and not _is_wrapper_agent_name(str(row.get("name") or ""))
        ]
    try:
        from meta_agent import DEFAULT_WEIGHTS
        names = list(DEFAULT_WEIGHTS)
        defaults = dict(DEFAULT_WEIGHTS)
    except Exception:
        names = []
        defaults = {}

    def _add_name(raw) -> None:
        for leaf in _leaf_agent_names(str(raw or "")):
            if leaf not in names:
                names.append(leaf)

    summary = _read_json(LOGS_DIR / "agent_summary.json", {})
    eval_data = _read_json(LOGS_DIR / "latest_eval.json", {})
    eval_agents = {
        a.get("name"): a
        for a in (eval_data.get("agents") or [])
        if isinstance(a, dict) and a.get("name")
    }
    if isinstance(summary, dict):
        for name in summary:
            _add_name(name)
    for name in eval_agents:
        _add_name(name)

    weights = _load_meta_weights()
    roster = []
    seen: set[str] = set()
    for name in names:
        if _is_wrapper_agent_name(name) or name in seen:
            continue
        seen.add(name)
        info = summary.get(name) if isinstance(summary, dict) else {}
        if not isinstance(info, dict):
            info = {}
        ev = eval_agents.get(name) or {}
        if "active" in info:
            status = "active" if info.get("active", True) else "benched"
        elif "active" in ev:
            status = "active" if ev.get("active", True) else "benched"
        else:
            status = "active"
        pnl = info.get("total_pnl")
        if pnl is None:
            pnl = ev.get("pnl_20d") if ev.get("pnl_20d") is not None else ev.get("pnl_alltime")
        w = weights.get(name)
        if w is None:
            w = defaults.get(name)
        if w is None:
            # Do not invent 1.00 for leftover compound labels.
            continue
        roster.append({
            "name": name,
            "status": status,
            "weight": float(w),
            "pnl": float(pnl or 0),
        })
    roster.sort(key=lambda r: r["pnl"], reverse=True)
    return roster


def scorecard_rotation_actions(today: str, d: dict | None = None) -> dict[str, list[str]]:
    """Today's FLAG / BENCHED / PROMOTED / REACTIVATED (always all four keys).

    FLAG comes from rotation_log AND evaluator ``latest_eval.json`` /
    ``d['flagged_today']`` so a FLAG that was not yet written to the log
    still renders on the scorecard.
    """
    d = d or {}
    buckets = {"FLAG": [], "BENCHED": [], "PROMOTED": [], "REACTIVATED": []}
    if "rotation_actions" in d:
        src = d.get("rotation_actions") or {}
        for k in buckets:
            buckets[k] = list(src.get(k) or [])
        extra = list(d.get("flagged_today") or [])
        have = " ".join(buckets["FLAG"]).lower()
        for name in extra:
            if name and name.lower() not in have:
                buckets["FLAG"].append(str(name))
                have += " " + name.lower()
        return buckets

    for rec in _rotations_today(today):
        ev = str(rec.get("event") or "").upper()
        if ev not in buckets:
            continue
        label = rec.get("description") or rec.get("agent") or ev
        if rec.get("agent") and rec["agent"] not in str(label):
            label = f"{rec['agent']} — {label}"
        buckets[ev].append(str(label))

    flagged = list(d.get("flagged_today") or [])
    eval_data = _read_json(LOGS_DIR / "latest_eval.json", {})
    gen = str(eval_data.get("generated_at") or "")
    if not flagged and today in gen:
        flagged = list(eval_data.get("flagged_agents") or [])
        for a in eval_data.get("agents") or []:
            if isinstance(a, dict) and a.get("flagged") and a.get("name"):
                if a["name"] not in flagged:
                    flagged.append(a["name"])
    existing = " ".join(buckets["FLAG"]).lower()
    for name in flagged:
        if name and name.lower() not in existing:
            buckets["FLAG"].append(str(name))
            existing += " " + name.lower()
    return buckets


# This bot is paper-only. TRADING_MODE=live / PAPER_TRADING=false cannot
# flip the report into a live column — that is how a paper loss got
# misread as a live one.
TRADING_MODE = "paper"

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
        "fetch_error_count": 0,
        "system_error_count": 0,
        "fetch_404_symbols": defaultdict(int),
        "raw_signal_ticks": [],     # list of (time_str, total_count)
        "synthesis_events": [],     # list of (time_str, raw, passed)
        "approved_batches": [],     # list of (time_str, count)
        "agent_signal_counts": defaultdict(int),  # cumulative across all today's ticks
        "regime_observations": [],  # list of regime strings observed
        "rejected_log_count": 0,
        "gate_events": defaultdict(int),
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
    # Current ensemble.py logs "Total raw signals: N". The old
    # ensemble_v11 line "Total raw signals from all agents:" is still
    # accepted so a mixed log after deploy still parses.
    re_raw_signals = re.compile(r"Total raw signals(?: from all agents)?:\s*(\d+)")
    re_synthesis = re.compile(
        r"(?:MetaAgent:\s*)?(\d+)\s+raw signals?\s*→\s*(\d+)\s+passed synthesis"
        r"|── Cycle:\s*(\d+)\s+raw\s*→\s*(\d+)\s+synthesized"
    )
    re_approved_batch = re.compile(r"Tick produced\s+(\d+)\s+approved signal")
    re_agent_count = re.compile(r"(\w+Agent):\s*(\d+)\s*signal")
    re_regime = re.compile(r"active regimes\s*=\s*\{([^}]+)\}")
    re_rejected = re.compile(r"(?:REJECTED|SKIPPED):\s+([A-Z][A-Z0-9\.\-]*)\s+")
    re_gate = re.compile(r"(Long entries blocked|Short entries blocked|Daily trade cap reached|GATE DEADLOCK|NAKED POSITIONS)")

    for line in today_lines:
        time_str = line[11:19] if len(line) > 19 else ""

        if "[ERROR]" in line:
            result["error_count"] += 1
            m = re_404_sym.search(line) or re_short_404.search(line)
            if m:
                result["fetch_404_symbols"][m.group(1)] += 1
            if _is_fetch_error(line):
                result["fetch_error_count"] += 1
            else:
                result["system_error_count"] += 1

        if "Ensemble cycle start" in line:
            result["tick_count"] += 1

        m = re_raw_signals.search(line)
        if m:
            result["raw_signal_ticks"].append((time_str, int(m.group(1))))

        m = re_synthesis.search(line)
        if m:
            raw = m.group(1) or m.group(3)
            passed = m.group(2) or m.group(4)
            if raw is not None and passed is not None:
                result["synthesis_events"].append((time_str, int(raw), int(passed)))

        m = re_approved_batch.search(line)
        if m:
            result["approved_batches"].append((time_str, int(m.group(1))))

        m = re_agent_count.search(line)
        if m:
            result["agent_signal_counts"][m.group(1)] += int(m.group(2))

        m = re_regime.search(line)
        if m:
            result["regime_observations"].append(m.group(1).strip())

        if re_rejected.search(line) or "⛔ REJECTED" in line or "SKIPPED:" in line:
            result["rejected_log_count"] += 1
        g = re_gate.search(line)
        if g:
            result["gate_events"][g.group(1)] += 1

    return result


def _is_fetch_error(line: str) -> bool:
    low = line.lower()
    return any(tok in low for tok in (
        "404", "yfinance", "http error", "no earnings", "timed out",
        "timeout", "rate limit", "too many requests",
    ))


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

    if sched.get("system_error_count", 0) > 20:
        findings.append(
            f"⚠️  {sched['system_error_count']} SYSTEM errors today "
            f"(plus {sched.get('fetch_error_count', 0)} fetch/404s). "
            f"Fetch errors are noise; system errors are the ones that "
            f"break ticks. Check `grep '\\[ERROR\\]' logs/scheduler.log`."
        )
    elif sched.get("fetch_error_count", 0) > 50:
        top_404 = sorted(sched["fetch_404_symbols"].items(), key=lambda x: -x[1])[:5]
        top_str = ", ".join(f"{s} ({n}×)" for s, n in top_404) or "n/a"
        findings.append(
            f"ℹ️  {sched['fetch_error_count']} fetch/404 errors today "
            f"(not counted as a health failure). Worst: {top_str}."
        )

    gates = sched.get("gate_events") or {}
    if gates.get("GATE DEADLOCK"):
        findings.append(
            "⚠️  Entry gates deadlocked (longs AND shorts blocked) — "
            "zero new trades were possible. Check gross/net exposure."
        )
    if gates.get("NAKED POSITIONS"):
        findings.append(
            "⚠️  CRITICAL: naked-position kill-switch fired — new entries "
            "were blocked until trailing stops could be re-armed."
        )
    if report["approved_count"] == 0 and report.get("raw_signals_total", 0) > 0:
        reasons = []
        if gates.get("Daily trade cap reached"):
            reasons.append("daily trade cap")
        if gates.get("Long entries blocked"):
            reasons.append("longs blocked by exposure")
        if gates.get("Short entries blocked"):
            reasons.append("shorts blocked (bull tape)")
        extra = f" Likely cause: {', '.join(reasons)}." if reasons else ""
        findings.append(
            f"ℹ️  Agents produced signals (peak {report['raw_signals_total']} raw) "
            f"but none were approved.{extra} "
            f"This is not a silent bot — it is gated."
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
        signals_today = []   # trade_log.jsonl is dead; ledger is authoritative
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
            shadow_pnl = _empty_pnl_summary('shadow P&L retired — broker snapshot is authoritative')
        live_pnl   = _empty_pnl_summary('live column retired — broker snapshot is authoritative')

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

        # --- Approved trades: read the LEDGER, not trade_log.jsonl ---
        # 2026-07-31: report said "No approved trades today" while the bot
        # had submitted AXTI and CVX. Cause: this section still read
        # trade_log.jsonl, the dead file nothing writes to — the same
        # stale source that made the weekly report show all zeros.
        ledger_today = []
        try:
            import trade_ledger as _lt
            for t in _lt.trades_on_date(_today_str()):
                ledger_today.append({
                    "time":        (t.opened_at_et or "")[11:19],
                    "symbol":      t.symbol,
                    "direction":   "long" if t.side == "LONG" else "short",
                    "agent":       str(t.primary_agent).replace("MetaAgent(", "").rstrip(")"),
                    "confidence":  None,
                    "risk_dollar": float(t.entry_price or 0) * float(t.shares or 0),
                })
        except Exception as _le:
            log_err = str(_le)

        approved_trades = list(ledger_today)
        total_notional = 0.0
        for a in (approved if not ledger_today else []):
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

        # --- Per-agent activity (leaf agents only — unwrap MetaAgent(...)) ---
        agent_activity: dict[str, dict] = {}
        for s in signals_today:
            leaves = _leaf_agent_names(s.get("agent", "Unknown"))
            if not leaves:
                continue
            for agent in leaves:
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
        agent_last_seen = {}  # dead source removed

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
            "approved_count":       len(approved_trades),
            "rejected_count":       sched.get("rejected_log_count", 0),
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
            "open_positions":       [],   # broker snapshot supplies this
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
        try:
            from report_data import snapshot as _snap
            report["snapshot"] = _snap()
            opened = report["snapshot"].get("opened_today") or []
            if opened:
                report["approved_count"] = len(opened)
        except Exception:
            report["snapshot"] = {}
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

    def _format_intelligence_section(self, d: dict) -> str:
        """Renders report_data.snapshot() — the single validated view.

        Rebuilt 2026-07-31. This section previously computed its own
        numbers from whichever source each block happened to pick, which
        produced three silent, flattering errors in one week ($22k ledger
        gap, missing trades, $6,960 one-day lag). It now renders only,
        and prints the snapshot's warnings at the top so a disagreement
        can never be resolved invisibly again.
        """
        try:
            from report_data import snapshot
            s = snapshot()
        except Exception as e:
            return f'<p style="color:#ef4444">Snapshot unavailable: {e}</p>'

        def clr(v):
            return "#22c55e" if (v or 0) >= 0 else "#ef4444"

        # Warnings first — loud, un-ignorable, above every number
        warn = ""
        if s.get("warnings"):
            items = "".join(f"<li style='margin:3px 0'>{w}</li>" for w in s["warnings"])
            warn = (f'<div style="background:#fef2f2;border-left:4px solid #ef4444;'
                    f'padding:12px 16px;border-radius:6px;margin:8px 0">'
                    f'<div style="font-weight:700;color:#991b1b;margin-bottom:4px">'
                    f'⚠️ Data integrity warnings</div>'
                    f'<ul style="margin:0;padding-left:18px;font-size:12px;color:#7f1d1d">'
                    f'{items}</ul></div>')

        if s.get("equity") is None:
            return warn + '<p style="color:#ef4444">Broker unreachable — no figures.</p>'

        head = (f'<div style="font-size:26px;font-weight:700;color:{clr(s["total_pnl"])}">'
                f'${s["equity"]:,.0f}</div>'
                f'<div style="font-size:12px;color:#64748b">account equity '
                f'({s["total_pct"]:+.2f}% since $100k) &nbsp;•&nbsp; today '
                f'<span style="color:{clr(s["day_pnl"])};font-weight:600">'
                f'${s["day_pnl"]:+,.0f}</span> &nbsp;•&nbsp; buying power '
                f'${s["buying_power"]:,.0f} &nbsp;•&nbsp; leverage '
                f'{s.get("leverage",0):.2f}x</div>')

        rows = "".join(
            f'<tr><td style="padding:6px 10px;font-weight:600">{w["label"]}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:{clr(w["chg"])};font-weight:700">'
            f'${w["chg"]:+,.0f} ({w["bot_pct"]:+.2f}%)</td>'
            f'<td style="padding:6px 10px;text-align:right;color:{clr(w["spy_pct"])}">'
            f'{w["spy_pct"]:+.2f}%</td>'
            f'<td style="padding:6px 10px;text-align:right;color:{clr(w["edge"])};font-weight:700">'
            f'{w["edge"]:+.2f}%</td></tr>' for w in s.get("windows", []))

        opened = "".join(
            f'<tr><td style="padding:5px 10px;font-size:12px">{t["time"]}</td>'
            f'<td style="padding:5px 10px;font-weight:600">{t["symbol"]}</td>'
            f'<td style="padding:5px 10px">{t["side"]}</td>'
            f'<td style="padding:5px 10px;text-align:right">${t["notional"]:,.0f}</td>'
            f'<td style="padding:5px 10px;font-size:11px;color:#64748b">{t["agent"][:34]}</td></tr>'
            for t in s.get("opened_today", [])) or             '<tr><td colspan="5" style="padding:8px 10px;color:#94a3b8">no entries today</td></tr>'

        closed = "".join(
            f'<tr><td style="padding:5px 10px;font-weight:600">{t["symbol"]}</td>'
            f'<td style="padding:5px 10px">{t["side"]}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:{clr(t["pnl"])};font-weight:700">'
            f'${t["pnl"]:+,.0f}</td>'
            f'<td style="padding:5px 10px;font-size:11px;color:#64748b">{t["agent"][:34]}</td></tr>'
            for t in s.get("closed_today", [])) or             '<tr><td colspan="4" style="padding:8px 10px;color:#94a3b8">no exits today</td></tr>'

        pos = "".join(
            f'<tr><td style="padding:5px 10px;font-weight:600">{p["symbol"]}</td>'
            f'<td style="padding:5px 10px">{p["side"]}{" OPT" if p["is_option"] else ""}</td>'
            f'<td style="padding:5px 10px;text-align:right">${p["mv"]:,.0f}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:{clr(p["unrl"])};font-weight:700">'
            f'${p["unrl"]:+,.0f}</td></tr>'
            for p in sorted(s.get("positions", []), key=lambda x: -x["unrl"]))

        th = 'style="padding:7px 10px;text-align:left"'
        thr = 'style="padding:7px 10px;text-align:right"'
        return f"""
    {warn}
    {head}
    <div class="section-title">🎯 Bot vs. Market — broker equity</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#1e293b;color:#fff"><th {th}>Window</th>
      <th {thr}>Bot</th><th {thr}>SPY</th><th {thr}>Edge</th></tr></thead>
      <tbody>{rows}</tbody></table>

    <div class="section-title">✅ Opened today</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#1e293b;color:#fff"><th {th}>Time</th>
      <th {th}>Symbol</th><th {th}>Side</th><th {thr}>Notional</th>
      <th {th}>Agent</th></tr></thead><tbody>{opened}</tbody></table>

    <div class="section-title">📕 Closed today</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#1e293b;color:#fff"><th {th}>Symbol</th>
      <th {th}>Side</th><th {thr}>P&amp;L</th><th {th}>Agent</th></tr></thead>
      <tbody>{closed}</tbody></table>

    <div class="section-title">📂 Open positions ({len(s.get('positions', []))}) — unrealized ${s.get('unrealized') or 0:+,.0f}</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#1e293b;color:#fff"><th {th}>Symbol</th>
      <th {th}>Side</th><th {thr}>Value</th><th {thr}>Unreal.</th></tr></thead>
      <tbody>{pos}</tbody></table>
"""

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
        """Per-agent attribution table: every leaf agent that contributed."""
        agents = [
            a for a in (d.get("agent_attribution") or [])
            if not _is_wrapper_agent_name(str(a.get("agent") or ""))
        ]
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

    def format_email_html(self, d: dict, snap: dict | None = None) -> str:
        """Short PAPER scorecard — not the old v2 diagnose dump."""
        sched = d.get("sched") or {}
        if snap is None:
            snap = d.get("snapshot") or {}

        def clr(v):
            return "#22c55e" if (v or 0) >= 0 else "#ef4444"

        eq = snap.get("equity")
        day = snap.get("day_pnl")
        day_pct = 0.0
        if eq and snap.get("prev_close"):
            day_pct = (float(eq) / float(snap["prev_close"]) - 1) * 100
        eq_html = "n/a" if eq is None else f"${eq:,.0f}"
        day_html = "n/a" if day is None else (
            f'<span style="color:{clr(day)}">${day:+,.0f} ({day_pct:+.2f}%)</span>'
        )

        windows = snap.get("windows") or []
        by_label = {w.get("label"): w for w in windows if w.get("label")}
        spy_order = ["1-day", "5-day", "Since start"]
        if "20-day" in by_label:
            spy_order = ["1-day", "5-day", "20-day", "Since start"]
        wrows = ""
        for label in spy_order:
            w = by_label.get(label)
            if not w:
                wrows += (
                    f'<tr><td style="padding:5px 8px">{label}</td>'
                    f'<td style="padding:5px 8px;text-align:right">—</td>'
                    f'<td style="padding:5px 8px;text-align:right">—</td>'
                    f'<td style="padding:5px 8px;text-align:right">—</td></tr>'
                )
                continue
            wrows += (
                f'<tr><td style="padding:5px 8px">{w["label"]}</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{clr(w["bot_pct"])}">'
                f'{w["bot_pct"]:+.2f}%</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{clr(w["spy_pct"])}">'
                f'{w["spy_pct"]:+.2f}%</td>'
                f'<td style="padding:5px 8px;text-align:right;color:{clr(w["edge"])};font-weight:700">'
                f'{w["edge"]:+.2f}%</td></tr>'
            )
        spy_table = (
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<thead><tr style="background:#1e293b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left">Window</th>'
            '<th style="padding:6px 8px;text-align:right">Bot</th>'
            '<th style="padding:6px 8px;text-align:right">SPY</th>'
            '<th style="padding:6px 8px;text-align:right">Edge</th>'
            '</tr></thead><tbody>'
            + (wrows or '<tr><td colspan="4" style="padding:8px;color:#94a3b8">benchmarks unavailable</td></tr>')
            + '</tbody></table>'
        )

        naked = snap.get("naked") or []
        ghosts = snap.get("ghosts") or []
        risk_html = (
            f'Naked exits: <b>{len(naked)}</b>'
            + (f' ({", ".join(naked[:6])})' if naked else " — protected")
            + f' &nbsp;•&nbsp; Ledger ghosts: {len(ghosts)}'
        )

        today = snap.get("today") or d.get("today") or _today_et().strftime("%Y-%m-%d")
        actions = scorecard_rotation_actions(today, d)
        act_lines = []
        for key in ("FLAG", "BENCHED", "PROMOTED", "REACTIVATED"):
            items = actions.get(key) or []
            body = "; ".join(items) if items else "none"
            act_lines.append(
                f'<div style="font-size:13px;margin:2px 0"><b>{key}:</b> {body}</div>'
            )
        rot_html = "".join(act_lines)

        roster = scorecard_agent_roster(d)
        if roster:
            trows = "".join(
                f'<tr><td style="padding:4px 8px;font-weight:600">{a["name"]}</td>'
                f'<td style="padding:4px 8px">{a.get("status") or ("active" if a.get("active") else "benched")}</td>'
                f'<td style="padding:4px 8px;text-align:right">{float(a.get("weight") or 0):.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:{clr(a.get("pnl"))}">'
                f'${float(a.get("pnl") or 0):+,.0f}</td></tr>'
                for a in roster
            )
        else:
            trows = (
                '<tr><td colspan="4" style="padding:8px;color:#94a3b8">'
                'no agent_summary.json / latest_eval.json yet</td></tr>'
            )
        roster_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:12px">'
            '<thead><tr style="background:#1e293b;color:#fff">'
            '<th style="padding:6px 8px;text-align:left">Agent</th>'
            '<th style="padding:6px 8px;text-align:left">Status</th>'
            '<th style="padding:6px 8px;text-align:right">Weight</th>'
            '<th style="padding:6px 8px;text-align:right">P&amp;L</th>'
            '</tr></thead><tbody>' + trows + '</tbody></table>'
            + '<p style="font-size:10px;color:#94a3b8;margin:6px 0 0;font-style:italic">'
              'Leaf agents only. MetaAgent(...) wrapper rows are omitted so '
              'soft weights (not a fake 1.00) drive kill/BENCH reads.</p>'
        )

        warns = snap.get("warnings") or []
        crit = [w for w in warns if "CRITICAL" in str(w).upper()]
        warn_html = ""
        if crit:
            warn_html = (
                '<div style="background:#fef2f2;border-left:4px solid #ef4444;'
                'padding:10px 12px;border-radius:6px;margin:8px 0;font-size:13px">'
                + "<br>".join(crit[:4]) + "</div>"
            )

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f8fafc; margin:0; padding:0; color:#1e293b; }}
  .wrapper {{ max-width:640px; margin:28px auto; background:#fff;
              border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }}
  .header {{ background:#0f172a; padding:22px 28px; }}
  .header h1 {{ color:#fff; margin:0; font-size:18px; font-weight:700; }}
  .header p  {{ color:#94a3b8; margin:4px 0 0; font-size:13px; }}
  .paper-banner {{ background:#1d4ed8; color:#fff; text-align:center;
                   font-size:13px; font-weight:700; letter-spacing:.08em;
                   padding:10px 16px; text-transform:uppercase; }}
  .body {{ padding:20px 28px; }}
  .section-title {{ font-size:12px; font-weight:700; color:#1e293b;
                    margin:18px 0 6px; text-transform:uppercase;
                    letter-spacing:0.04em; }}
  .footer {{ background:#f1f5f9; padding:12px 28px; font-size:11px;
             color:#94a3b8; text-align:center; }}
</style></head><body>
<div class="wrapper">
  <div class="paper-banner">PAPER TRADING — Alpaca paper account — not live</div>
  <div class="header">
    <h1>Market day scorecard</h1>
    <p>{d.get('date_display') or today}  •  {d.get('generated_at') or ''}  •  Mode: <strong style="color:#93c5fd">PAPER</strong></p>
  </div>
  <div class="body">
    {warn_html}
    <div style="font-size:28px;font-weight:700">{eq_html}</div>
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">equity &nbsp;•&nbsp; day {day_html}</div>
    <div class="section-title">Bot vs SPY</div>
    {spy_table}
    <div class="section-title">Open risk</div>
    <p style="font-size:13px;color:#475569;margin:0">{risk_html}</p>
    <div class="section-title">Today FLAG / BENCHED / PROMOTED / REACTIVATED</div>
    {rot_html}
    <div class="section-title">Agents (active / benched / weight / P&amp;L)</div>
    {roster_html}
    <div class="section-title">Errors / signals truth</div>
    <p style="font-size:13px;color:#475569;margin:0">
      Ticks: {sched.get('tick_count', 0):,} / ~390 &nbsp;•&nbsp;
      System errors: {sched.get('system_error_count', 0):,} &nbsp;•&nbsp;
      Fetch/404s: {sched.get('fetch_error_count', 0):,}
      &nbsp;•&nbsp; Entries today: {d.get('approved_count', 0)}
      &nbsp;•&nbsp; Peak raw signals: {d.get('raw_signals_total', 0)}
      &nbsp;•&nbsp; Last log: {sched.get("last_log") or "—"}
    </p>
  </div>
  <div class="footer">
    BluSterling &amp; Associates LLC • PAPER TRADING only. Live trading is not enabled.<br>
    One email on NYSE open days. Slack is CRITICAL heal failures only.
  </div>
</div></body></html>"""

    # ── Send ──────────────────────────────────────────────────────────────────

    def send(self, html: str, subject: str | None = None) -> bool:
        if not daily_email_enabled():
            print("ENABLE_DAILY_EMAIL=false — skip send")
            return True
        if not GMAIL_ADDRESS or not GMAIL_APP_PW:
            print("ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
            return False
        subject = subject or format_email_subject()
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
            print(f"Market-day scorecard sent to {REPORT_TO_EMAIL}: {subject}")
            return True
        except Exception as e:
            print(f"Failed to send email: {e}")
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
        send_now = "--send-now" in sys.argv
        reason = skip_send_reason(send_now=send_now)
        if reason:
            print(reason)
            sys.exit(0)
        reporter = DailyReporter()
        data     = reporter.build_report()
        html     = reporter.format_email_html(data)
        reporter.save_html(html)
        if send_now:
            snap = data.get("snapshot")
            if snap is None:
                try:
                    from report_data import snapshot as _snap
                    snap = _snap()
                except Exception:
                    snap = {}
            subject = format_email_subject(snap)
            ok = reporter.send(html, subject=subject)
            if not ok:
                _crash_log(RuntimeError(
                    f"reporter.send() returned False. "
                    f"GMAIL_ADDRESS empty: {not GMAIL_ADDRESS}. "
                    f"GMAIL_APP_PW empty: {not GMAIL_APP_PW}. "
                    f".env path checked: {BASE_DIR / '.env'}"
                ))
                sys.exit(1)
        else:
            print("Scorecard generated (HTML saved). Pass --send-now to email it.")
    except Exception as e:
        _crash_log(e)
        print(f"Reporter crashed: {e}", file=sys.stderr)
        sys.exit(1)
