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
        log_path = os.path.join(os.path.dirname(__file__), "logs", "scheduler.log")
        approved_today = rejected_today = errors_today = 0
        try:
            with open(log_path) as f:
                today_str = datetime.now(ET).strftime("%Y-%m-%d")
                for line in f:
                    if today_str not in line:
                        continue
                    if "APPROVED" in line:
                        approved_today += 1
                    elif "REJECTED" in line:
                        rejected_today += 1
                    elif "[ERROR]" in line:
                        errors_today += 1
        except Exception:
            pass

        from alpaca_stream import is_streaming
        stream_status = "Live Alpaca stream" if is_streaming() else "yfinance fallback"
        status_icon = "✅" if errors_today == 0 else "⚠️"

        text = (
            f"{status_icon} *BluSterling Daily Summary — {datetime.now(ET).strftime('%a %b %d')}*\n"
            f"• Signals approved: *{approved_today}*\n"
            f"• Signals rejected: {rejected_today}\n"
            f"• Errors: {errors_today}\n"
            f"• Data source: {stream_status}\n"
            f"_Next run: Mon–Fri 9:30 AM ET_"
        )
        if errors_today > 0:
            text += f"\n⚠️ *{errors_today} error(s) today — check logs*"

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
            summary_key = f"summary_{now.date()}"
            if (now.hour == SUMMARY_TIME_ET[0]
                    and now.minute == SUMMARY_TIME_ET[1]
                    and summary_key not in _eval_done_this_window):
                _eval_done_this_window.add(summary_key)
                post_daily_slack_summary()

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
