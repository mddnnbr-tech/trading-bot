"""Unit tests for the Sep 2026 risk / reporting / health fixes.

No broker, no alpaca-py, no network. These lock the detection helpers and
the reporter log-format contract so a v11 regex cannot silently report
"0 signals" again.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class NakedAndGhostDetection(unittest.TestCase):
    def test_long_without_sell_is_naked(self):
        from invariants import naked_equity_symbols
        positions = [{"symbol": "AMD", "qty": "10"}]
        orders = [{"symbol": "AMD", "side": "buy"}]  # wrong side
        self.assertEqual(naked_equity_symbols(positions, orders), ["AMD"])

    def test_long_with_sell_is_protected(self):
        from invariants import naked_equity_symbols
        positions = [{"symbol": "AMD", "qty": 10}]
        orders = [{"symbol": "AMD", "side": "sell"}]
        self.assertEqual(naked_equity_symbols(positions, orders), [])

    def test_short_needs_buy_to_protect(self):
        from invariants import naked_equity_symbols
        positions = [{"symbol": "META", "qty": -5}]
        self.assertEqual(
            naked_equity_symbols(positions, [{"symbol": "META", "side": "sell"}]),
            ["META"],
        )
        self.assertEqual(
            naked_equity_symbols(positions, [{"symbol": "META", "side": "buy"}]),
            [],
        )

    def test_crypto_and_options_excluded(self):
        from invariants import naked_equity_symbols
        positions = [
            {"symbol": "BTCUSD", "qty": 0.1},
            {"symbol": "AAPL250117C00200000", "qty": 1},
            {"symbol": "BAC", "qty": 20},
        ]
        self.assertEqual(naked_equity_symbols(positions, []), ["BAC"])

    def test_ghost_ignores_crypto_and_options(self):
        from invariants import ghost_symbols
        ghosts = ghost_symbols(
            ["SUNB", "BTC/USD", "AAPL250117C00200000", "AMD"],
            ["AMD"],
        )
        self.assertEqual(ghosts, ["SUNB"])


class PaperOnlyLock(unittest.TestCase):
    def test_default_is_paper(self):
        from invariants import paper_only_violation
        env = {k: v for k, v in os.environ.items()
               if k not in ("PAPER_TRADING", "TRADING_MODE")}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(paper_only_violation())

    def test_refuses_live_env(self):
        from invariants import paper_only_violation
        with patch.dict(os.environ, {"PAPER_TRADING": "false"}, clear=False):
            self.assertIn("paper-only", paper_only_violation())
        with patch.dict(os.environ, {"PAPER_TRADING": "true",
                                     "TRADING_MODE": "live"}, clear=False):
            self.assertIn("paper-only", paper_only_violation())

    def test_executor_source_hardwires_paper_client(self):
        import inspect
        import order_executor
        src = inspect.getsource(order_executor.OrderExecutor.__init__)
        self.assertIn("paper=True", src)
        self.assertNotIn("paper=PAPER_TRADING", src)


class ReporterLogParse(unittest.TestCase):
    def test_parses_current_ensemble_lines_not_v11(self):
        import daily_reporter as dr
        today = dr._today_str()
        text = "\n".join([
            f"{today} 09:31:00,000 [INFO] Ensemble cycle start 09:31:00 UTC",
            f"{today} 09:31:01,000 [INFO] Total raw signals: 12",
            f"{today} 09:31:02,000 [INFO] MetaAgent: 12 raw signals → 3 passed synthesis (conf_mult=1.00)",
            f"{today} 09:31:03,000 [INFO] ── Cycle: 12 raw → 3 synthesized → 1 approved ──",
            f"{today} 09:31:04,000 [INFO] Tick produced 1 approved signal(s)",
            f"{today} 09:31:05,000 [ERROR] HTTP Error 404: yfinance",
            f"{today} 09:31:06,000 [ERROR] Order submission failed for AMD: boom",
            f"{today} 09:31:07,000 [INFO] 📉 Long entries blocked — net long 110%",
            f"{today} 09:31:08,000 [INFO] ⛔ REJECTED: AAPL  — Confidence 0.40 below minimum",
        ])
        tmp = Path(tempfile.mkdtemp())
        (tmp / "scheduler.log").write_text(text)
        orig = dr.LOGS_DIR
        dr.LOGS_DIR = tmp
        try:
            r = dr.read_scheduler_today()
        finally:
            dr.LOGS_DIR = orig
        self.assertTrue(r["ran"])
        self.assertEqual(r["tick_count"], 1)
        self.assertEqual(r["raw_signal_ticks"][0][1], 12)
        self.assertEqual(r["synthesis_events"][0][1:], (12, 3))
        self.assertEqual(r["approved_batches"][0][1], 1)
        self.assertGreaterEqual(r["fetch_error_count"], 1)
        self.assertGreaterEqual(r["system_error_count"], 1)
        self.assertGreaterEqual(r["rejected_log_count"], 1)
        self.assertIn("Long entries blocked", r["gate_events"])

    def test_email_html_is_obviously_paper(self):
        import daily_reporter as dr
        reporter = dr.DailyReporter.__new__(dr.DailyReporter)
        data = {
            "date_display": "Thursday, September 10, 2026",
            "generated_at": "2026-09-10 16:35 ET",
            "trading_mode": "paper",
            "total_pnl": 0,
            "approved_count": 2,
            "rejected_count": 4,
            "raw_signals_total": 12,
            "passed_synthesis": 3,
            "total_notional": 0,
            "findings": ["ℹ️  Agents produced signals but none were approved."],
            "ledger_status": {"available": True, "added": 0, "total": 0, "refresh": {}},
            "sched": {
                "tick_count": 100, "error_count": 5,
                "system_error_count": 1, "fetch_error_count": 4,
                "last_log": "16:00:00", "fetch_404_symbols": {},
            },
            "approved_trades": [],
            "rejection_reasons": {},
            "agent_activity": {},
            "shadow_pnl": dr._empty_pnl_summary(),
            "live_pnl": dr._empty_pnl_summary(),
            "snapshot": {
                "today": "2026-09-10",
                "equity": 98765,
                "prev_close": 100000,
                "day_pnl": -1235,
                "naked": ["AMD"],
                "ghosts": [],
                "windows": [
                    {"label": "1-day", "bot_pct": -1.24, "spy_pct": 0.10, "edge": -1.34},
                    {"label": "5-day", "bot_pct": -2.00, "spy_pct": 0.50, "edge": -2.50},
                    {"label": "Since start", "bot_pct": -1.24, "spy_pct": 8.00, "edge": -9.24},
                ],
                "warnings": [],
            },
            "agent_roster": [
                {"name": "NewsAgent", "status": "active", "weight": 1.00, "pnl": 210},
                {"name": "MomentumAgent", "status": "benched", "weight": 0.15, "pnl": -80},
                {"name": "EarningsAgent", "status": "active", "weight": 0.40, "pnl": 12},
            ],
            "flagged_today": ["MomentumAgent"],
            "rotation_actions": {
                "FLAG": ["MomentumAgent — 20d P&L below ensemble"],
                "BENCHED": ["MomentumAgent"],
                "PROMOTED": ["BreakoutAgent"],
                "REACTIVATED": ["EarningsAgent"],
            },
        }
        html = reporter.format_email_html(data)
        self.assertIn("PAPER TRADING", html)
        self.assertIn("not live", html.lower())
        self.assertIn("System errors: 1", html)
        self.assertIn("Entries today: 2", html)
        self.assertIn("Peak raw signals: 12", html)
        self.assertIn("Bot vs SPY", html)
        self.assertIn("Naked exits:", html)
        self.assertIn("Weight", html)
        self.assertIn("FLAG", html)
        self.assertIn("BENCHED", html)
        self.assertIn("PROMOTED", html)
        self.assertIn("REACTIVATED", html)
        self.assertIn("MomentumAgent", html)
        self.assertIn("benched", html)
        self.assertIn("active", html)
        self.assertNotIn("Top 3 agents", html)
        self.assertNotIn("Daily Report v2", html)
        self.assertNotIn("v11", html.lower())

    def test_scorecard_renders_evaluator_flags_without_rotation_log(self):
        import daily_reporter as dr
        actions = dr.scorecard_rotation_actions("2026-09-11", {
            "flagged_today": ["BreakoutAgent", "TechnicalAgent"],
        })
        self.assertIn("BreakoutAgent", actions["FLAG"])
        self.assertIn("TechnicalAgent", actions["FLAG"])
        self.assertEqual(actions["BENCHED"], [])
        self.assertEqual(actions["PROMOTED"], [])
        self.assertEqual(actions["REACTIVATED"], [])

    def test_subject_line_is_paper_scorecard(self):
        import daily_reporter as dr
        subj = dr.format_email_subject({
            "today": "2026-09-11",
            "equity": 98765.4,
            "prev_close": 100000,
        })
        self.assertEqual(
            subj,
            "[PAPER] Market day — 2026-09-11 — equity $98,765 (day -1.23%)",
        )

    def test_kill_switch_and_holiday_skip_send(self):
        import daily_reporter as dr
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        with patch.dict(os.environ, {"ENABLE_DAILY_EMAIL": "false"}, clear=False):
            self.assertIn("ENABLE_DAILY_EMAIL=false", dr.skip_send_reason(send_now=True) or "")
        saturday = datetime(2026, 9, 12, 16, 35, tzinfo=ET)
        with patch.object(dr, "daily_email_enabled", return_value=True):
            with patch.object(dr, "is_open_market_report_day", return_value=False):
                self.assertIn("market closed", dr.skip_send_reason(send_now=True) or "")
        self.assertIsNone(dr.skip_send_reason(send_now=False))
        # holiday helper: Thanksgiving 2026 is a Thursday
        turkey = datetime(2026, 11, 26, 16, 35, tzinfo=ET)
        self.assertFalse(dr.is_open_market_report_day(turkey))
        self.assertFalse(dr.is_open_market_report_day(saturday))


class TrailQtyHelper(unittest.TestCase):
    def test_partial_fill_qty_is_int(self):
        from order_executor import _order_qty
        self.assertEqual(_order_qty(181.0), 181)
        self.assertEqual(_order_qty("181"), 181)
        self.assertEqual(_order_qty(0), 0)


class EnsureExitsBackstop(unittest.TestCase):
    def test_submits_trail_only_for_naked_equity(self):
        from order_executor import ensure_protective_exits, _protect_failed_at
        _protect_failed_at.clear()

        class _Pos:
            def __init__(self, symbol, qty):
                self.symbol, self.qty = symbol, qty
                self.unrealized_plpc = 0

        class _Ord:
            def __init__(self, symbol, side):
                self.symbol, self.side = symbol, side

        class _Client:
            def __init__(self):
                self.submitted = []
            def get_all_positions(self):
                return [_Pos("AMD", 10), _Pos("BAC", 5)]
            def get_orders(self, _req=None):
                return [_Ord("BAC", "sell")]  # BAC already protected
            def submit_order(self, req):
                self.submitted.append(req)
                class _O:
                    id = "trail-1"
                return _O()
            def get_open_position(self, symbol):
                return _Pos(symbol, 10)

        client = _Client()

        class _Req:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _Enums:
            class QueryOrderStatus:
                OPEN = "open"
            class OrderSide:
                SELL = "sell"
                BUY = "buy"
            class TimeInForce:
                GTC = "gtc"

        import types, sys
        fake_requests = types.ModuleType("alpaca.trading.requests")
        fake_requests.TrailingStopOrderRequest = _Req
        fake_requests.GetOrdersRequest = _Req
        fake_enums = types.ModuleType("alpaca.trading.enums")
        fake_enums.QueryOrderStatus = _Enums.QueryOrderStatus
        fake_enums.OrderSide = _Enums.OrderSide
        fake_enums.TimeInForce = _Enums.TimeInForce
        fake_trading = types.ModuleType("alpaca.trading")
        fake_alpaca = types.ModuleType("alpaca")
        with patch("order_executor.paper_only_violation", return_value=None):
            with patch("order_executor.get_executor") as ge:
                ge.return_value._client = client
                with patch.dict(sys.modules, {
                    "alpaca": fake_alpaca,
                    "alpaca.trading": fake_trading,
                    "alpaca.trading.requests": fake_requests,
                    "alpaca.trading.enums": fake_enums,
                }):
                    result = ensure_protective_exits(client)
        self.assertEqual(result["protected"], ["AMD"])
        self.assertEqual(result["still_naked"], [])
        self.assertEqual(len(client.submitted), 1)


class SlackHardDisabled(unittest.TestCase):
    """Slack outbound is a hard no-op — env/webhook cannot re-enable it."""

    FAKE_HOOK = "https://hooks.slack.com/services/T00/B00/FAKE"

    def test_helpers_never_http_post(self):
        import slack_notify
        self.assertFalse(slack_notify.ENABLE_SLACK_SUMMARY)
        self.assertFalse(slack_notify.slack_outbound_enabled())
        self.assertEqual(slack_notify.SLACK_WEBHOOK_URL, "")
        with patch.dict(os.environ, {
            "SLACK_WEBHOOK_URL": self.FAKE_HOOK,
            "ENABLE_SLACK_SUMMARY": "true",
        }):
            with patch("urllib.request.urlopen") as urlopen:
                with patch("urllib.request.Request") as req:
                    ok = slack_notify.post_text(
                        "CRITICAL: test", webhook_url=self.FAKE_HOOK)
                    ok2 = slack_notify.post_webhook(
                        self.FAKE_HOOK, {"text": "CRITICAL: test"})
        self.assertFalse(ok)
        self.assertFalse(ok2)
        urlopen.assert_not_called()
        req.assert_not_called()

    def test_scheduler_summary_never_http_post(self):
        import inspect
        import market_scheduler as ms
        self.assertFalse(ms.ENABLE_SLACK_SUMMARY)
        self.assertEqual(ms.SLACK_WEBHOOK, "")
        src = inspect.getsource(ms.post_daily_slack_summary)
        self.assertNotIn("urlopen", src)
        with patch.dict(os.environ, {
            "SLACK_WEBHOOK_URL": self.FAKE_HOOK,
            "ENABLE_SLACK_SUMMARY": "true",
        }):
            with patch("urllib.request.urlopen") as urlopen:
                ms.post_daily_slack_summary()
        urlopen.assert_not_called()

    def test_health_critical_never_slacks(self):
        import inspect
        import health_check as hc
        self.assertEqual(hc.SLACK_WEBHOOK, "")
        src = inspect.getsource(hc.send_alert)
        self.assertNotIn("urlopen", src)
        with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": self.FAKE_HOOK}):
            with patch("urllib.request.urlopen") as urlopen:
                with patch.object(hc, "GMAIL_ADDRESS", ""):
                    with patch.object(hc, "GMAIL_APP_PW", ""):
                        hc.send_alert(["CRITICAL: scheduler.log missing"])
        urlopen.assert_not_called()

    def test_health_critical_still_emails(self):
        import health_check as hc
        with patch.object(hc, "GMAIL_ADDRESS", "a@b.com"):
            with patch.object(hc, "GMAIL_APP_PW", "pw"):
                with patch.object(hc, "REPORT_TO_EMAIL", "a@b.com"):
                    with patch("urllib.request.urlopen") as urlopen:
                        with patch("health_check.smtplib.SMTP_SSL") as smtp:
                            client = smtp.return_value.__enter__.return_value
                            hc.send_alert(["CRITICAL: bot dead"])
        urlopen.assert_not_called()
        smtp.assert_called()
        client.sendmail.assert_called()


if __name__ == "__main__":
    unittest.main()
