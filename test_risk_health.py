"""Unit tests for the Sep 2026 risk / reporting / health fixes.

No broker, no alpaca-py, no network. These lock the detection helpers and
the reporter log-format contract so a v11 regex cannot silently report
"0 signals" again.
"""

from __future__ import annotations

import json
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
        }
        with patch.object(reporter, "_format_intelligence_section",
                          return_value="<p>snapshot</p>"):
            html = reporter.format_email_html(data)
        self.assertIn("PAPER TRADING", html)
        self.assertIn("not live", html.lower())
        self.assertIn("System errors: 1", html)
        self.assertIn("Entries today: 2", html)
        self.assertIn("Peak raw signals: 12", html)
        self.assertIn("Agents (active / benched / weight", html)
        self.assertNotIn(">MetaAgent(", html)


class ScorecardLeafRoster(unittest.TestCase):
    def test_expand_unwraps_meta_wrapper(self):
        from trade_ledger import expand_agent_names, is_wrapper_agent_name
        self.assertEqual(
            expand_agent_names("MetaAgent(NewsAgent, OptionsFlowAgent)"),
            ["NewsAgent", "OptionsFlowAgent"],
        )
        self.assertEqual(expand_agent_names("NewsAgent"), ["NewsAgent"])
        self.assertEqual(expand_agent_names("MetaAgent"), [])
        self.assertEqual(expand_agent_names("BrokerSync"), [])
        self.assertTrue(is_wrapper_agent_name("MetaAgent(NewsAgent)"))
        self.assertTrue(is_wrapper_agent_name("MetaAgent"))
        self.assertFalse(is_wrapper_agent_name("NewsAgent"))

    def test_injected_roster_drops_wrappers(self):
        import daily_reporter as dr
        roster = dr.scorecard_agent_roster({
            "agent_roster": [
                {"name": "MetaAgent(NewsAgent, OptionsFlowAgent)",
                 "status": "active", "weight": 1.0, "pnl": 10},
                {"name": "NewsAgent", "status": "active", "weight": 0.42, "pnl": -100},
                {"name": "MetaAgent", "status": "active", "weight": 1.0, "pnl": 999},
            ]
        })
        names = [r["name"] for r in roster]
        self.assertEqual(names, ["NewsAgent"])
        self.assertAlmostEqual(roster[0]["weight"], 0.42)

    def test_summary_wrappers_do_not_get_fake_weight_1(self):
        import daily_reporter as dr
        tmp = Path(tempfile.mkdtemp())
        (tmp / "agent_summary.json").write_text(json.dumps({
            "MetaAgent(NewsAgent, OptionsFlowAgent)": {
                "active": True, "total_pnl": 50,
            },
            "NewsAgent": {"active": True, "total_pnl": -200},
        }))
        orig = dr.LOGS_DIR
        dr.LOGS_DIR = tmp
        try:
            with patch.object(dr, "_load_meta_weights",
                              return_value={"NewsAgent": 0.31}):
                roster = dr.scorecard_agent_roster({})
        finally:
            dr.LOGS_DIR = orig
        names = [r["name"] for r in roster]
        self.assertNotIn("MetaAgent(NewsAgent, OptionsFlowAgent)", names)
        self.assertNotIn("MetaAgent", names)
        self.assertTrue(all(not n.startswith("MetaAgent") for n in names))
        news = next(r for r in roster if r["name"] == "NewsAgent")
        self.assertAlmostEqual(news["weight"], 0.31)

    def test_email_weight_table_lists_leaves_not_wrappers(self):
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
            "findings": [],
            "ledger_status": {"available": True, "added": 0, "total": 0, "refresh": {}},
            "sched": {
                "tick_count": 100, "error_count": 5,
                "system_error_count": 1, "fetch_error_count": 4,
                "last_log": "16:00:00", "fetch_404_symbols": {},
            },
            "approved_trades": [],
            "rejection_reasons": {},
            "agent_activity": {
                "MetaAgent(MomentumAgent, BreakoutAgent)": {
                    "approved": 2, "rejected": 0, "total": 2,
                },
                "MomentumAgent": {"approved": 1, "rejected": 0, "total": 1},
            },
            "agent_roster": [
                {"name": "MetaAgent(MomentumAgent)", "status": "active",
                 "weight": 1.0, "pnl": 1},
                {"name": "MomentumAgent", "status": "active",
                 "weight": 0.55, "pnl": 12},
            ],
            "shadow_pnl": dr._empty_pnl_summary(),
            "live_pnl": dr._empty_pnl_summary(),
        }
        with patch.object(reporter, "_format_intelligence_section",
                          return_value="<p>snapshot</p>"):
            html = reporter.format_email_html(data)
        self.assertIn("MomentumAgent", html)
        self.assertIn("0.55", html)
        self.assertNotIn(">MetaAgent(", html)
        self.assertNotIn("MetaAgent(MomentumAgent)", html)
        self.assertNotIn("MetaAgent(MomentumAgent, BreakoutAgent)", html)


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


if __name__ == "__main__":
    unittest.main()
