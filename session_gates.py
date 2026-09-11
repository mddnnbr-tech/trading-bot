"""
session_gates.py
────────────────
Single source of truth for WHEN this bot may open new risk.

This fork is paper-only equities during NYSE regular trading hours.
Crypto new entries are hard-disabled. After-hours the scheduler may
monitor, heal, and queue — it must not submit equity entries.

Every entry path (ensemble, order_executor, crypto_scheduler) must call
these helpers rather than inventing its own clock or symbol check.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("SessionGates")

ET = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)

# Hard paper-only. This fork must never flip to live via a missing .env.
PAPER_ONLY = True

# Crypto sleeve is OFF in this bot. Existing BTC/ETH/SOL positions may
# still be monitored/exited; no new crypto entries are submitted.
CRYPTO_TRADING_ENABLED = False

# Alpaca crypto symbols in both slash and compact forms.
CRYPTO_SYMBOLS = frozenset({
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LTC/USD",
    "BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "DOGEUSD", "LTCUSD",
    "BTC-USD", "ETH-USD", "SOL-USD",
})

_NYSE = None
_CALENDAR_AVAILABLE = False
try:
    import pandas_market_calendars as mcal
    _NYSE = mcal.get_calendar("NYSE")
    _CALENDAR_AVAILABLE = True
except ImportError:
    pass

# Used when pandas_market_calendars is not installed (this env, some VMs).
# Observed NYSE closures 2025–2027 including weekend-shifted Independence /
# Christmas / Juneteenth. Good Friday included (not computable from weekday).
_NYSE_HOLIDAYS_FALLBACK = frozenset({
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def is_paper_mode() -> bool:
    try:
        from invariants import paper_only_violation
        return paper_only_violation() is None
    except Exception:
        flag = os.getenv("PAPER_TRADING", "true").strip().lower()
        return flag in ("1", "true", "yes", "on")


def assert_paper_only(context: str = "") -> None:
    """Refuse to proceed if live trading was requested. Paper-safe hard stop."""
    try:
        from invariants import paper_only_violation
        msg = paper_only_violation()
    except Exception:
        msg = None if is_paper_mode() else (
            "PAPER-ONLY fork refused to start: PAPER_TRADING is not true. "
            "This bot will not submit live orders."
        )
    if not msg:
        return
    if context:
        msg = f"{context}: {msg}"
    log.critical(msg)
    raise RuntimeError(msg)


def is_crypto_symbol(symbol: str | None) -> bool:
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if s in CRYPTO_SYMBOLS:
        return True
    compact = s.replace("/", "").replace("-", "")
    if compact in CRYPTO_SYMBOLS:
        return True
    if "/" in s and s.endswith("USD"):
        return True
    # Alpaca compact crypto (BTCUSD / ETHUSD / SOLUSD). Known bases only —
    # do not treat an equity ticker that happens to end in USD as crypto.
    if compact.endswith("USD") and len(compact) >= 6 and compact[:-3].isalpha():
        return compact[:-3] in {"BTC", "ETH", "SOL", "AVAX", "DOGE", "LTC", "DOT", "LINK", "UNI"}
    return False


def is_nyse_session_day(now: datetime | None = None) -> bool:
    """True on a weekday that is not an NYSE holiday."""
    now = now_et(now)
    if now.weekday() >= 5:
        return False
    if _CALENDAR_AVAILABLE and _NYSE is not None:
        date_str = now.strftime("%Y-%m-%d")
        try:
            schedule = _NYSE.schedule(start_date=date_str, end_date=date_str)
            if schedule.empty:
                return False
            return True
        except Exception:
            pass
    return now.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS_FALLBACK


def is_rth(now: datetime | None = None) -> bool:
    """True during NYSE regular trading hours: 09:30–16:00 America/New_York."""
    now = now_et(now)
    if not is_nyse_session_day(now):
        return False
    open_dt = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                          second=0, microsecond=0)
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                           second=0, microsecond=0)
    return open_dt <= now < close_dt


def is_after_hours_monitor_window(now: datetime | None = None) -> bool:
    """Weekday session day, outside RTH, when we still monitor/heal.

    Pre-market 04:00–09:30 ET and after-hours 16:00–20:00 ET. Weekends
    and holidays are out — no equity book to babysit on a closed calendar
    except a once-per-loop heal that the scheduler can still call.
    """
    now = now_et(now)
    if not is_nyse_session_day(now):
        return False
    if is_rth(now):
        return False
    minutes = now.hour * 60 + now.minute
    pre = 4 * 60 <= minutes < (9 * 60 + 30)
    post = 16 * 60 <= minutes < 20 * 60
    return pre or post


def crypto_entries_allowed() -> bool:
    return bool(CRYPTO_TRADING_ENABLED)


def equity_entries_allowed(now: datetime | None = None, symbol: str | None = None) -> tuple[bool, str]:
    """May we open a NEW equity (or crypto) position right now?

    Returns (allowed, reason). reason is empty when allowed.
    """
    assert_paper_only("equity_entries_allowed")
    if symbol and is_crypto_symbol(symbol):
        if not CRYPTO_TRADING_ENABLED:
            return False, "crypto sleeve disabled in this bot"
        return False, "crypto entries are not taken by the equity loop"
    if not is_rth(now):
        return False, "equities RTH-only (09:30–16:00 America/New_York)"
    return True, ""
