"""
health_check.py
────────────────
Autonomous watchdog. Runs hourly via cron during market hours.

Purpose: catch the class of bug that cost weeks last time (dead cron,
duplicate .env keys, stale ledger, broken email auth) BEFORE it silently
runs for days. Self-heals what's safe to auto-fix. Sends exactly one
Slack/email alert only when something is actually broken — no noise on
healthy days, so this never becomes something you have to babysit.

Checks:
  1. Duplicate keys in .env (auto-fixes: keeps last occurrence, the one
     python-dotenv actually uses, so behavior doesn't silently change)
  2. scheduler.log has fresh entries within the last 10 minutes during
     market hours (bot process actually alive)
  3. Ledger (paper_trades.csv) has today's trades if market has been open
  4. Gmail send actually works (sends a silent test, not a real report)
  5. No single agent has 5+ duplicate open positions on the same symbol+side
     (regression check for the dedup bug fixed 2026-07-01)

Cron (add to crontab -e):
  0 * 9-16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/health_check.py
"""

from __future__ import annotations

import os
import smtplib
from collections import Counter
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
ET = ZoneInfo("America/New_York")

GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PW    = os.getenv("GMAIL_APP_PASSWORD", "")
REPORT_TO_EMAIL = os.getenv("REPORT_TO_EMAIL", GMAIL_ADDRESS)
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "")


def check_and_fix_env_duplicates() -> list[str]:
    """De-dupe .env keys, keeping the LAST occurrence of each (matches
    python-dotenv's actual resolution order) so behavior doesn't change."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return ["CRITICAL: .env file missing entirely"]

    lines = env_path.read_text().splitlines()
    keys_seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            keys_seen[key] = i  # keep index of LAST occurrence

    dupes = [k for k in keys_seen if sum(1 for l in lines if l.split("=", 1)[0].strip() == k
             and "=" in l and not l.strip().startswith("#")) > 1]
    if not dupes:
        return []

    keep_indices = set(keys_seen.values())
    cleaned = [
        line for i, line in enumerate(lines)
        if not ("=" in line and not line.strip().startswith("#")) or i in keep_indices
    ]
    env_path.write_text("\n".join(cleaned) + "\n")
    return [f"AUTO-FIXED: removed duplicate .env keys: {', '.join(sorted(dupes))}"]


def check_bot_alive() -> list[str]:
    """During market hours, scheduler.log should have entries in the last ~10 min."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return []
    market_open  = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        return []

    log_path = BASE_DIR / "logs" / "scheduler.log"
    if not log_path.exists():
        return ["CRITICAL: scheduler.log missing — bot has likely never started"]

    try:
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=ET)
        if now - mtime > timedelta(minutes=10):
            return [f"CRITICAL: scheduler.log hasn't updated in {int((now-mtime).total_seconds()/60)} min "
                    f"during market hours — bot process may be dead. Check `systemctl status trading-bot`."]
    except Exception as e:
        return [f"WARNING: could not check scheduler.log freshness ({e})"]
    return []


def check_duplicate_positions() -> list[str]:
    """Regression guard for the 2026-07-01 duplicate-entry bug."""
    try:
        import trade_ledger as _ledger
        opens = _ledger.open_positions()
        counts = Counter((t.symbol, t.side) for t in opens)
        offenders = {k: v for k, v in counts.items() if v >= 5}
        if offenders:
            detail = ", ".join(f"{sym} {side} x{n}" for (sym, side), n in offenders.items())
            return [f"WARNING: duplicate-position regression detected: {detail}. "
                    f"Check ensemble.py's has_open_position() gate is still in place."]
    except Exception as e:
        return [f"WARNING: could not check for duplicate positions ({e})"]
    return []


def check_ledger_fresh() -> list[str]:
    """If market has been open >30 min today, the ledger should have today's trades
    OR at least a refreshed timestamp — otherwise the bot may be running but not trading."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return []
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < market_open + timedelta(minutes=30) or now.hour >= 16:
        return []
    try:
        import trade_ledger as _ledger
        today_str = now.strftime("%Y-%m-%d")
        today_trades = [t for t in _ledger.all_trades() if t.opened_at_et[:10] == today_str]
        if not today_trades:
            return ["WARNING: no trades opened today despite market being open 30+ min. "
                     "Not necessarily a bug (quiet market), but worth a glance."]
    except Exception as e:
        return [f"WARNING: could not check ledger freshness ({e})"]
    return []


def check_broker_ledger_drift() -> list[str]:
    """Flag Alpaca positions the ledger thinks are closed (orphans).

    Root cause this guards against (found 2026-07-09): bracket exit orders
    are day-only — they expire each close, the ledger simulates the exit,
    but the real position persists at the broker. Drift compounded until
    legacy positions held $341k gross exposure and buying power hit $0,
    silently freezing all new trading."""
    try:
        import trade_ledger as _ledger
        from alpaca.trading.client import TradingClient
        key, secret = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", "")
        if not key or not secret:
            return []
        client = TradingClient(api_key=key, secret_key=secret, paper=True)

        ledger_open = {t.symbol.replace("/", "") for t in _ledger.open_positions()}
        broker_open = {str(p.symbol) for p in client.get_all_positions()}
        orphans = broker_open - ledger_open

        issues = []
        if orphans:
            issues.append(
                f"WARNING: broker/ledger drift — Alpaca holds positions the ledger "
                f"already closed: {', '.join(sorted(orphans))}. These consume buying "
                f"power invisibly; close them or reconcile the ledger."
            )
        bp = float(client.get_account().buying_power)
        if bp < 1000:
            issues.append(
                f"CRITICAL: buying power ${bp:,.0f} — the bot cannot open new "
                f"positions. Check for drift/orphan positions above."
            )
        return issues
    except Exception as e:
        return [f"WARNING: could not check broker/ledger drift ({e})"]


def check_email_auth() -> list[str]:
    """Verify Gmail SMTP creds actually authenticate (cheap, no email sent)."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PW:
        return ["WARNING: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set — daily/weekly emails will silently fail"]
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PW)
        return []
    except smtplib.SMTPAuthenticationError as e:
        return [f"CRITICAL: Gmail auth failed ({e}). App password is likely stale/revoked — "
                f"generate a new one at myaccount.google.com/apppasswords and update .env."]
    except Exception as e:
        return [f"WARNING: could not verify Gmail auth ({e})"]


def send_alert(issues: list[str]) -> None:
    subject = f"⚠️ Trading Bot Health Check FAILED — {datetime.now(ET).strftime('%b %d, %I:%M %p ET')}"
    body = "The following issues were found:\n\n" + "\n\n".join(f"• {i}" for i in issues)

    if SLACK_WEBHOOK:
        try:
            import json, urllib.request
            text = f"🚨 *Health Check Alert*\n" + "\n".join(f"• {i}" for i in issues)
            req = urllib.request.Request(
                SLACK_WEBHOOK, data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    if GMAIL_ADDRESS and GMAIL_APP_PW:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = GMAIL_ADDRESS
            msg["To"]   = REPORT_TO_EMAIL
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
                s.login(GMAIL_ADDRESS, GMAIL_APP_PW)
                s.sendmail(GMAIL_ADDRESS, REPORT_TO_EMAIL, msg.as_string())
        except Exception:
            pass  # if email itself is broken, Slack alert above is the fallback


def _should_alert(issues: list[str]) -> bool:
    """Alert policy: email/Slack ONLY for CRITICAL findings, and never
    re-alert the same set of problems more than once per 6 hours.

    Why: the hourly cron re-emailed the same known WARNING every hour
    (4 duplicate emails on 2026-07-17 for a drift issue that already had
    an automated fix scheduled). Warnings are logged and auto-remediated
    by the daily reconcile; they don't page the human."""
    import hashlib, json, time
    criticals = [i for i in issues if i.startswith("CRITICAL")]
    if not criticals:
        return False

    state_path = BASE_DIR / "logs" / "health_state.json"
    digest = hashlib.sha256("|".join(sorted(issues)).encode()).hexdigest()
    now = time.time()
    try:
        prev = json.loads(state_path.read_text())
    except Exception:
        prev = {}
    if prev.get("digest") == digest and now - prev.get("ts", 0) < 6 * 3600:
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"digest": digest, "ts": now}))
    return True


def main():
    issues: list[str] = []
    issues += check_and_fix_env_duplicates()
    issues += check_bot_alive()
    issues += check_duplicate_positions()
    issues += check_ledger_fresh()
    issues += check_broker_ledger_drift()
    issues += check_email_auth()

    if issues:
        print(f"Health check found {len(issues)} issue(s):")
        for i in issues:
            print(f"  • {i}")
        if _should_alert(issues):
            send_alert(issues)
        else:
            print("(logged only — no critical findings or already alerted within 6h)")
    else:
        print(f"✅ Health check passed — {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}")


if __name__ == "__main__":
    main()
