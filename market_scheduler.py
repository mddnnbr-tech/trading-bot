"""
market_scheduler.py
───────────────────
Main entry point for the cloud-hosted trading loop.

• Runs ONLY during US market hours: Mon–Fri 09:30–16:00 ET
• Skips NYSE holidays automatically (uses pandas_market_calendars)
• Every TICK_SECONDS, runs the agent ensemble and logs results
• Twice daily (10:00 AM and 3:30 PM ET), runs the evaluation + rotation cycle
• On SIGTERM/SIGINT, shuts down cleanly

Deploy on Google Cloud VM (see CLOUD_SETUP.md):
  python market_scheduler.py

Or run as a systemd service so it auto-starts on VM reboot:
  see cloud_setup_guide.md for the unit file
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Optional: pandas_market_calendars for holiday-aware scheduling ──────────
try:
    import pandas_market_calendars as mcal
    _NYSE = mcal.get_calendar("NYSE")
    _CALENDAR_AVAILABLE = True
except ImportError:
    _CALENDAR_AVAILABLE = False

from agent_evaluator import AgentEvaluator
from agent_rotator   import AgentRotator

# ── Config ───────────────────────────────────────────────────────────────────
ET              = ZoneInfo("America/New_York")
MARKET_OPEN     = (9, 30)    # hour, minute ET
MARKET_CLOSE    = (16, 0)    # hour, minute ET
TICK_SECONDS    = 60         # how often the main loop fires
EVAL_TIMES_ET   = [(10, 0), (15, 30)]   # twice-daily evaluation windows
LEARN_TIME_ET   = (15, 45)              # weekly learning run: Fridays at 3:45 PM ET
SUMMARY_TIME_ET = (15, 55)             # daily Slack summary: 3:55 PM ET

SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL   = "#trading-alerts"

# ── Logging setup ────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "scheduler.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scheduler")

# ── Graceful shutdown ────────────────────────────────────────────────────────
_running = True

def _handle_shutdown(signum, frame):
    global _running
    log.info("Shutdown signal received — finishing current tick then stopping.")
    _running = False

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)


# ── Market hours helpers ──────────────────────────────────────────────────────

def is_market_open(now: datetime) -> bool:
    """True if 'now' (timezone-aware) is within NYSE trading hours."""
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False

    # Holiday check (requires pandas_market_calendars)
    if _CALENDAR_AVAILABLE:
        date_str = now.strftime("%Y-%m-%d")
        schedule = _NYSE.schedule(start_date=date_str, end_date=date_str)
        if schedule.empty:
            return False   # holiday

    market_open  = now.replace(hour=MARKET_OPEN[0],  minute=MARKET_OPEN[1],  second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)

    return market_open <= now < market_close


def is_eval_time(now: datetime) -> bool:
    """True if 'now' matches one of the twice-daily evaluation windows (within 1 minute)."""
    for hour, minute in EVAL_TIMES_ET:
        window_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_end   = now.replace(hour=hour, minute=minute + 1, second=0, microsecond=0)
        if window_start <= now < window_end:
            return True
    return False


# ── Agent ensemble (Phase C — wired) ─────────────────────────────────────────

def run_agent_tick():
    """Execute one full ensemble cycle: signals → risk bridge → paper/live orders."""
    now = datetime.now(ET)
    log.debug(f"Agent tick at {now.strftime('%H:%M:%S ET')}")
    try:
        # Also refresh open positions in the ledger every 5 minutes
        if now.minute % 5 == 0:
            try:
                import trade_ledger as _ledger
                result = _ledger.refresh_open_positions()
                log.debug(f"Ledger refresh: {result}")
            except Exception as le:
                log.debug(f"Ledger refresh failed: {le}")

        from ensemble import run_ensemble
        approved = run_ensemble()
        if approved:
            log.info(f"Tick produced {len(approved)} approved signal(s)")
    except Exception as e:
        log.error(f"Ensemble tick failed: {e}", exc_info=True)


def post_daily_slack_summary():
    """Post end-of-day summary to Slack #trading-alerts at 3:55 PM ET."""
    if not SLACK_WEBHOOK:
        log.debug("SLACK_WEBHOOK_URL not set — skipping daily summary")
        return
    try:
        import json, urllib.request, urllib.error

        today_str = datetime.now(ET).strftime("%Y-%m-%d")

        # ── Pull real trade data from ledger ──────────────────────────────
        realized = unrealized = 0.0
        total_trades = wins = losses = open_count = 0
        best_trade = worst_trade = None
        try:
            import trade_ledger as _ledger
            all_t = _ledger.all_trades()
            today_t = [t for t in all_t if t.opened_at_et.startswith(today_str)]
            closed_t = [t for t in today_t if not t.is_open]
            open_t   = [t for t in today_t if t.is_open]
            realized   = sum(t.realized_pnl or 0 for t in closed_t)
            unrealized = sum(t.unrealized_pnl or 0 for t in open_t)
            total_trades = len(today_t)
            wins   = sum(1 for t in closed_t if (t.realized_pnl or 0) > 0)
            losses = sum(1 for t in closed_t if (t.realized_pnl or 0) <= 0)
            open_count = len(open_t)

            if closed_t:
                best_trade  = max(closed_t, key=lambda t: t.realized_pnl or 0)
                worst_trade = min(closed_t, key=lambda t: t.realized_pnl or 0)
        except Exception:
            pass

        # ── Error count from log ──────────────────────────────────────────
        errors_today = 0
        log_path = os.path.join(os.path.dirname(__file__), "logs", "scheduler.log")
        try:
            with open(log_path) as f:
                for line in f:
                    if today_str in line and "[ERROR]" in line:
                        errors_today += 1
        except Exception:
            pass

        try:
            from alpaca_stream import is_streaming
            stream_status = "Alpaca stream ✅" if is_streaming() else "yfinance fallback"
        except Exception:
            stream_status = "unknown"

        pnl_total = realized + unrealized
        pnl_sign  = "+" if pnl_total >= 0 else ""
        pnl_emoji = "📈" if pnl_total >= 0 else "📉"
        status_icon = "✅" if errors_today < 10 else "⚠️"

        lines = [
            f"{status_icon} *BluSterling Daily Summary — {datetime.now(ET).strftime('%a %b %d, %Y')}*",
            f"",
            f"{pnl_emoji} *Total P&L: {pnl_sign}${pnl_total:,.2f}*  _(realized: {'+' if realized>=0 else ''}${realized:,.2f} | open: {'+' if unrealized>=0 else ''}${unrealized:,.2f})_",
            f"• Trades today: *{total_trades}*  ({wins}W / {losses}L closed, {open_count} still open)",
        ]

        if best_trade and (best_trade.realized_pnl or 0) > 0:
            lines.append(f"🏆 Best: *{best_trade.symbol}* {best_trade.side}  +${best_trade.realized_pnl:,.2f}")
        if worst_trade and (worst_trade.realized_pnl or 0) < 0:
            lines.append(f"📉 Worst: *{worst_trade.symbol}* {worst_trade.side}  ${worst_trade.realized_pnl:,.2f}")

        lines += [
            f"",
            f"• Data: {stream_status}  |  Errors: {errors_today}",
            f"_Next run: Mon–Fri 9:30 AM ET_",
        ]

        text = "\n".join(lines)

        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Daily Slack summary posted.")
    except Exception as e:
        log.warning(f"Slack summary failed: {e}")


def send_daily_email():
    """Send end-of-day recap email via Gmail SMTP."""
    try:
        import smtplib, json
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        gmail   = os.getenv("GMAIL_ADDRESS", "")
        pw      = os.getenv("GMAIL_APP_PASSWORD", "")
        to      = os.getenv("REPORT_TO_EMAIL", gmail)
        if not gmail or not pw:
            log.debug("Gmail creds not set — skipping email")
            return
        import trade_ledger as _ledger
        trades   = _ledger.all_trades()
        today    = datetime.now(ET).strftime("%Y-%m-%d")
        today_t  = [t for t in trades if t.opened_at_et.startswith(today)]
        open_t   = [t for t in today_t if t.is_open]
        closed_t = [t for t in today_t if not t.is_open]
        realized = sum(t.realized_pnl or 0 for t in closed_t)
        unreal   = sum(t.unrealized_pnl or 0 for t in open_t)
        rows = "".join(
            f"<tr><td>{t.symbol}</td><td>{t.side}</td>"
            f"<td>${t.entry_price:.2f}</td><td>{t.status}</td>"
            f"<td style='color:{'green' if (t.realized_pnl or t.unrealized_pnl or 0)>=0 else 'red'}'>"
            f"${(t.realized_pnl or t.unrealized_pnl or 0):+.2f}</td></tr>"
            for t in today_t
        )
        html = f"""<html><body>
        <h2>BluSterling Trading Bot — {datetime.now(ET).strftime('%b %d, %Y')}</h2>
        <p><b>Realized P&L:</b> <span style="color:{'green' if realized>=0 else 'red'}">${realized:+.2f}</span> &nbsp;
           <b>Unrealized:</b> ${unreal:+.2f} &nbsp;
           <b>Trades today:</b> {len(today_t)}</p>
        <table border="1" cellpadding="4" style="border-collapse:collapse">
        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Status</th><th>P&L</th></tr>
        {rows if rows else '<tr><td colspan=5>No trades today</td></tr>'}
        </table>
        <p style="color:gray;font-size:12px">BluSterling & Associates LLC — paper trading</p>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Trading Bot — {datetime.now(ET).strftime('%b %d')} | P&L ${realized:+.2f}"
        msg["From"]    = gmail
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail, pw)
            s.sendmail(gmail, to, msg.as_string())
        log.info(f"Daily recap email sent to {to}")
    except Exception as e:
        log.warning(f"Email send failed: {e}")


def sync_alpaca_positions():
    """
    Pull closed orders from Alpaca and update trade_ledger with realized P&L.
    Runs at market close so the strategy learner has real data each Friday.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        import trade_ledger as _ledger

        key    = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_API_SECRET", "")
        if not key or not secret:
            return
        client = TradingClient(api_key=key, secret_key=secret, paper=True)
        req    = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
        orders = client.get_orders(req)
        trades = _ledger.load_ledger()
        updated = 0
        for order in orders:
            sym  = str(order.symbol)
            side = "LONG" if str(order.side) == "buy" else "SHORT"
            for tid, t in trades.items():
                if t.symbol == sym and t.side == side and t.is_open:
                    filled = float(order.filled_avg_price or 0)
                    if filled > 0:
                        pnl = (filled - t.entry_price) * t.shares
                        if side == "SHORT":
                            pnl = -pnl
                        t.status          = "target" if pnl >= 0 else "stop"
                        t.exit_price      = filled
                        t.exit_at_et      = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
                        t.realized_pnl    = round(pnl, 2)
                        t.unrealized_pnl  = 0.0
                        updated += 1
                        break
        if updated:
            _ledger.save_ledger(trades)
            log.info(f"Alpaca sync: updated {updated} closed position(s) in ledger")

        # ── Auto-reconcile drift: close broker positions the ledger has
        # already booked as exited. The ledger's simulated exits and the
        # broker's trailing-stop exits are different engines; when the
        # simulation closes first, the real position lingers and consumes
        # buying power invisibly (this froze the account on 2026-07-08).
        try:
            ledger_open = {t.symbol.replace("/", "") for t in _ledger.open_positions()}
            for p in client.get_all_positions():
                sym = str(p.symbol)
                if sym not in ledger_open:
                    # Stale exit orders (old brackets/trails) hold the qty
                    # and make close_position fail with "insufficient qty
                    # available" — cancel them first, then liquidate.
                    try:
                        stale = client.get_orders(GetOrdersRequest(
                            status=QueryOrderStatus.OPEN, symbols=[sym]))
                        for o in stale:
                            client.cancel_order_by_id(o.id)
                    except Exception:
                        pass
                    client.close_position(sym)
                    log.info(f"Reconcile: closed orphan broker position {sym} "
                             f"(ledger already exited it)")
        except Exception as e:
            log.warning(f"Orphan reconcile failed: {e}")
    except Exception as e:
        log.warning(f"Alpaca position sync failed: {e}")


def run_learning_cycle():
    """Run the strategy learner — Fridays at 3:45 PM ET."""
    log.info("=" * 60)
    log.info("Running weekly strategy learning cycle...")
    try:
        from strategy_learner import StrategyLearner
        learner = StrategyLearner()
        result  = learner.learn()
        log.info(
            f"Learning complete: win_rate={result.get('overall_win_rate', 'N/A')} "
            f"pnl=${result.get('overall_pnl', 0):+.2f}"
        )
    except Exception as e:
        log.error(f"Strategy learning error: {e}", exc_info=True)
    log.info("=" * 60)


# ── Evaluation cycle ──────────────────────────────────────────────────────────

_eval_done_this_window: set[str] = set()

def run_eval_cycle():
    """Run evaluation + rotation. Called twice per market day."""
    log.info("═" * 60)
    log.info("Starting evaluation and rotation cycle...")
    try:
        evaluator = AgentEvaluator()
        report    = evaluator.evaluate()
        evaluator.save_report(report)
        log.info("\n" + report.summary_text())

        if report.flagged_agents:
            log.info(f"Flagged agents detected: {report.flagged_agents} — running rotation...")
            rotator = AgentRotator()
            result  = rotator.run_rotation()
            log.info(f"Rotation actions: {result['actions']}")
        else:
            log.info("All agents within performance threshold — no rotation needed.")
    except Exception as e:
        log.error(f"Evaluation cycle error: {e}", exc_info=True)
    log.info("═" * 60)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║     Trading Bot Market Scheduler — Starting      ║")
    log.info("╚══════════════════════════════════════════════════╝")
    log.info(f"Market hours: {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d} – "
             f"{MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d} ET  |  Tick: {TICK_SECONDS}s")

    global _running
    last_tick_minute = -1

    while _running:
        now = datetime.now(ET)

        if is_market_open(now):
            # ── Agent tick (once per minute) ──────────────────────────────
            current_minute = now.hour * 60 + now.minute
            if current_minute != last_tick_minute:
                last_tick_minute = current_minute
                run_agent_tick()

            # ── Twice-daily evaluation ────────────────────────────────────
            window_key = f"{now.date()}_{now.hour}_{now.minute}"
            if is_eval_time(now) and window_key not in _eval_done_this_window:
                _eval_done_this_window.add(window_key)
                run_eval_cycle()

            # ── Weekly learning run (Fridays only at 3:45 PM ET) ─────────
            learn_key = f"learn_{now.date()}"
            if (now.weekday() == 4  # Friday
                    and now.hour == LEARN_TIME_ET[0]
                    and now.minute == LEARN_TIME_ET[1]
                    and learn_key not in _eval_done_this_window):
                _eval_done_this_window.add(learn_key)
                run_learning_cycle()

            # ── Daily Slack summary at 3:55 PM ET ────────────────────────
            # Email is deliberately NOT sent here — daily_reporter.py's cron
            # job at 4:35 PM sends the one comprehensive daily email. Sending
            # both meant two emails a day covering overlapping info, which
            # was the actual cause of notification overload (not a missing
            # setting). One Slack ping + one email per day, that's it.
            summary_key = f"summary_{now.date()}"
            if (now.hour == SUMMARY_TIME_ET[0]
                    and now.minute == SUMMARY_TIME_ET[1]
                    and summary_key not in _eval_done_this_window):
                _eval_done_this_window.add(summary_key)
                post_daily_slack_summary()
                sync_alpaca_positions()

        else:
            # Outside market hours — sleep longer to conserve resources
            now_str = now.strftime("%a %Y-%m-%d %H:%M ET")
            if now.second < TICK_SECONDS:  # log once per tick period
                log.debug(f"Market closed ({now_str}) — waiting...")
            time.sleep(TICK_SECONDS * 5)
            continue

        time.sleep(TICK_SECONDS)

    log.info("Scheduler stopped cleanly.")


if __name__ == "__main__":
    main()
