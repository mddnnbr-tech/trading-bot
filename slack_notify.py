"""Hard-disabled Slack outbound for the paper trading bot.

Every helper is a no-op. SLACK_WEBHOOK_URL and ENABLE_SLACK_SUMMARY are
ignored so a leftover VM .env cannot page Slack — including CRITICAL
health alerts. Email / log paths stay elsewhere.
"""
from __future__ import annotations

import logging

log = logging.getLogger("slack_notify")

# Hardcoded off. Env cannot re-enable outbound Slack.
ENABLE_SLACK_SUMMARY = False
SLACK_WEBHOOK_URL = ""


def slack_outbound_enabled() -> bool:
    return False


def post_text(text: str, webhook_url: str | None = None) -> bool:
    """No-op. Never HTTP-posts, even if a webhook is passed or set in env."""
    log.info("Slack outbound hard-disabled — dropping message")
    return False


def post_webhook(url: str, payload: dict | None = None) -> bool:
    """No-op. Never HTTP-posts."""
    log.info("Slack outbound hard-disabled — dropping webhook payload")
    return False
