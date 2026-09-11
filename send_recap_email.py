"""
send_recap_email.py — RETIRED duplicate daily emailer.

Do not cron this file. The only market-day email is:

    35 16 * * 1-5  python3 daily_reporter.py --send-now

That script skips weekends/NYSE holidays and is gated by ENABLE_DAILY_EMAIL.
If this file is still in crontab (~16:30), remove that line — it used to
double-fire with daily_reporter.py (~16:35) as "Trading Bot — Daily Recap".
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("send_recap_email")


def main() -> int:
    msg = (
        "send_recap_email.py is a no-op (duplicate daily email killed). "
        "Remove this crontab line. Single sender: daily_reporter.py --send-now "
        "(ENABLE_DAILY_EMAIL; NYSE session days only)."
    )
    print(msg)
    log.info(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
