"""
send_recap_email.py — Daily paper trading recap emailer
Run this from your trading-bot folder after market close each day.

Usage:
    python send_recap_email.py

Reads credentials from .env automatically.
Reads today's logs and sends a plain-text recap to REPORT_TO_EMAIL.
"""

import json
import os
import smtplib
import ssl
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


# ── Load .env ────────────────────────────────────────────────────────────────

def load_env(env_path: Path) -> dict:
    """Parse a .env file into a dict. Does not touch os.environ."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


BASE_DIR   = Path(__file__).parent
ENV        = load_env(BASE_DIR / ".env")
LOG_DIR    = BASE_DIR / "logs"

GMAIL_ADDRESS   = ENV.get("GMAIL_ADDRESS", "")
GMAIL_PASSWORD  = ENV.get("GMAIL_APP_PASSWORD", "")
REPORT_TO_EMAIL = ENV.get("REPORT_TO_EMAIL", GMAIL_ADDRESS)
TODAY           = date.today()
TODAY_STR       = TODAY.strftime("%Y-%m-%d")          # for log matching
TODAY_DISPLAY   = TODAY.strftime("%B %-d, %Y")        # e.g. "April 13, 2026"
TODAY_SHORT     = TODAY.strftime("%b %-d")            # e.g. "Apr 13"


# ── Read logs ────────────────────────────────────────────────────────────────

def read_trade_log() -> list[dict]:
    """Return today's entries from trade_log.jsonl."""
    path = LOG_DIR / "trade_log.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            ts = record.get("timestamp", "")
            if TODAY_STR in ts:
                entries.append(record)
        except json.JSONDecodeError:
            continue
    return entries


def read_agent_summary() -> dict:
    """Return per-agent P&L summary."""
    path = LOG_DIR / "agent_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def scheduler_ran_today() -> bool:
    """Return True if scheduler.log has a line dated today."""
    path = LOG_DIR / "scheduler.log"
    if not path.exists():
        return False
    try:
        content = path.read_text()
        return TODAY_STR in content
    except Exception:
        return False


def read_latest_eval() -> dict:
    """Return latest_eval.json if it exists, else {}."""
    path = LOG_DIR / "latest_eval.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


# ── Build email body ─────────────────────────────────────────────────────────

def build_body(
    ran: bool,
    trades: list[dict],
    summary: dict,
    eval_data: dict,
) -> str:

    total_signals  = len(trades)
    approved       = [t for t in trades if t.get("status") == "approved"]
    rejected       = [t for t in trades if t.get("status") == "rejected"]
    total_pnl      = sum(
        agent.get("total_pnl", 0.0) for agent in summary.values()
    )
    pnl_str        = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"

    # Active agents
    active_agents = [name for name, data in summary.items() if data.get("active")]

    if not ran:
        # ── Scheduler did not fire ────────────────────────────────────────────
        body = f"""Hey Baker,

Quick update on your trading bot for {TODAY_DISPLAY}.

It looks like the scheduler didn't fire today — the trade log and scheduler log are both empty. The most likely cause is that market_scheduler.py wasn't started this morning. Totally understandable; this stuff takes a few days to get into a rhythm.

Current state:
  Signals generated : 0
  Approved trades   : 0
  Paper P&L         : $0.00
  Risk rejections   : none (nothing came through to reject)

All {len(active_agents)} agents ({', '.join(active_agents)}) are active and properly configured — they're just waiting for the first run.

To trade tomorrow, open a terminal in your project folder and run:
    python market_scheduler.py

If you'd rather not do this manually each morning, the Google Cloud VM will handle it automatically — happy to walk you through that setup whenever you're ready.

Weekly reports start next Monday — you're all set."""

    elif total_signals == 0:
        # ── Ran but no signals ────────────────────────────────────────────────
        body = f"""Hey Baker,

Quick recap from your trading bot on {TODAY_DISPLAY}.

The scheduler ran today but no signals were generated — the market may not have offered setups that met the agents' criteria, or it's possible data feeds were quiet. This is normal, especially early on.

Summary:
  Signals generated : 0
  Approved trades   : 0
  Paper P&L         : $0.00
  Agents active     : {len(active_agents)} ({', '.join(active_agents)})

System health looks good — no errors, agents are online.

Weekly reports start next Monday — you're all set."""

    else:
        # ── Actual trading activity ───────────────────────────────────────────

        # Which agents fired signals?
        agent_counts: dict[str, int] = {}
        for t in trades:
            agent = t.get("agent", "Unknown")
            agent_counts[agent] = agent_counts.get(agent, 0) + 1

        agent_lines = "\n".join(
            f"  {agent}: {count} signal{'s' if count != 1 else ''}"
            for agent, count in sorted(agent_counts.items(), key=lambda x: -x[1])
        )

        # Rejection reasons
        rejection_summary = ""
        if rejected:
            reasons: dict[str, int] = {}
            for t in rejected:
                reason = t.get("rejection_reason", "unspecified")
                reasons[reason] = reasons.get(reason, 0) + 1
            rejection_lines = "; ".join(
                f"{count}x {reason}" for reason, count in reasons.items()
            )
            rejection_summary = f"\n  Rejected          : {len(rejected)} ({rejection_lines})"

        # Eval note
        eval_note = ""
        if eval_data:
            score = eval_data.get("overall_score")
            if score is not None:
                eval_note = f"\n  Eval score        : {score:.1f}/10"

        body = f"""Hey Baker,

Here's your trading bot recap for {TODAY_DISPLAY}.

Top line:
  Signals generated : {total_signals}
  Approved trades   : {len(approved)}
  Paper P&L         : {pnl_str}{rejection_summary}{eval_note}

Agent activity:
{agent_lines}

System health looks good — all agents responded and the risk bridge is running.

Weekly reports start next Monday — you're all set."""

    return body


# ── Send email ───────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_PASSWORD:
        raise ValueError(
            "GMAIL_ADDRESS or GMAIL_APP_PASSWORD missing from .env"
        )

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = REPORT_TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, REPORT_TO_EMAIL, msg.as_string())


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ran     = scheduler_ran_today()
    trades  = read_trade_log()
    summary = read_agent_summary()
    eval_d  = read_latest_eval()

    subject = f"Trading Bot — Daily Recap ({TODAY_SHORT})"
    body    = build_body(ran, trades, summary, eval_d)

    print(f"Sending recap to {REPORT_TO_EMAIL}...")
    send_email(subject, body)
    print(f"✓ Sent: {subject}")


if __name__ == "__main__":
    main()
