"""Learn / rotate / RTH / daily-email gates. No broker, no network."""

from __future__ import annotations

import inspect
import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class SessionGates(unittest.TestCase):
    def test_weekend_is_not_session_day(self):
        from session_gates import is_nyse_session_day, is_rth, equity_entries_allowed
        sat = datetime(2026, 9, 12, 12, 0, tzinfo=ET)
        self.assertFalse(is_nyse_session_day(sat))
        self.assertFalse(is_rth(sat))
        ok, reason = equity_entries_allowed(sat, "AMD")
        self.assertFalse(ok)
        self.assertIn("RTH-only", reason)

    def test_rth_weekday_allows_equity(self):
        from session_gates import is_rth, equity_entries_allowed
        tue = datetime(2026, 9, 8, 10, 15, tzinfo=ET)
        self.assertTrue(is_rth(tue))
        with patch.dict(os.environ, {"PAPER_TRADING": "true", "TRADING_MODE": "paper"},
                        clear=False):
            ok, reason = equity_entries_allowed(tue, "AMD")
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_crypto_entries_blocked(self):
        from session_gates import is_crypto_symbol, equity_entries_allowed, CRYPTO_TRADING_ENABLED
        self.assertTrue(is_crypto_symbol("BTC/USD"))
        self.assertTrue(is_crypto_symbol("ETHUSD"))
        self.assertFalse(is_crypto_symbol("AMD"))
        self.assertFalse(CRYPTO_TRADING_ENABLED)
        tue = datetime(2026, 9, 8, 10, 15, tzinfo=ET)
        with patch.dict(os.environ, {"PAPER_TRADING": "true", "TRADING_MODE": "paper"},
                        clear=False):
            ok, reason = equity_entries_allowed(tue, "BTC/USD")
        self.assertFalse(ok)
        self.assertIn("crypto", reason.lower())

    def test_paper_lock_refuses_live_env(self):
        from session_gates import assert_paper_only
        with patch.dict(os.environ, {"PAPER_TRADING": "false"}, clear=False):
            with self.assertRaises(RuntimeError):
                assert_paper_only("test")
        with patch.dict(os.environ, {"PAPER_TRADING": "true", "TRADING_MODE": "live"},
                        clear=False):
            with self.assertRaises(RuntimeError):
                assert_paper_only("test")


class LedgerAgentNames(unittest.TestCase):
    def test_unwraps_meta_agent_compound(self):
        from trade_ledger import expand_agent_names
        self.assertEqual(
            expand_agent_names("MetaAgent(NewsAgent, OptionsFlowAgent)"),
            ["NewsAgent", "OptionsFlowAgent"],
        )
        self.assertEqual(expand_agent_names("NewsAgent"), ["NewsAgent"])
        self.assertEqual(expand_agent_names("BrokerSync"), [])
        self.assertEqual(expand_agent_names("MetaAgent"), [])


class SlackAndDuplicateEmailOff(unittest.TestCase):
    def test_slack_summary_defaults_off(self):
        import market_scheduler as ms
        src = inspect.getsource(ms.post_daily_slack_summary)
        self.assertIn('ENABLE_SLACK_SUMMARY', src)
        self.assertIn('"false"', src)
        with patch.dict(os.environ, {"ENABLE_SLACK_SUMMARY": "false",
                                     "SLACK_WEBHOOK_URL": "https://hooks.example/fake"},
                        clear=False):
            with patch.object(ms.log, "info") as info:
                ms.post_daily_slack_summary()
        joined = " ".join(str(c) for c in info.call_args_list)
        self.assertIn("OFF", joined)

    def test_scheduler_email_is_noop(self):
        import market_scheduler as ms
        src = inspect.getsource(ms.send_daily_email)
        self.assertIn("no-op", src)
        with patch.object(ms.log, "info") as info:
            ms.send_daily_email()
        self.assertIn("no-op", str(info.call_args))

    def test_send_recap_is_noop(self):
        import send_recap_email as recap
        self.assertEqual(recap.main(), 0)
        self.assertNotIn("send_email", recap.main.__code__.co_names)

    def test_health_alerts_critical_only(self):
        import health_check as hc
        src = inspect.getsource(hc.main)
        self.assertIn('startswith("CRITICAL")', src)
        self.assertTrue(callable(hc.check_email_auth))


class LearnerWiring(unittest.TestCase):
    def test_ensemble_calls_get_agent_adjustment(self):
        import ensemble
        src = inspect.getsource(ensemble.Ensemble)
        self.assertIn("get_agent_adjustment", src)
        self.assertIn("_apply_learned_adjustments", src)
        self.assertIn("get_worst_symbols", src)

    def test_risk_bridge_can_raise_confidence_floor(self):
        import agent_risk_bridge
        src = inspect.getsource(agent_risk_bridge)
        self.assertIn("get_agent_adjustment", src)
        self.assertIn("confidence_threshold_delta", src)

    def test_mean_reversion_in_default_weights(self):
        from meta_agent import DEFAULT_WEIGHTS
        self.assertIn("MeanReversionAgent", DEFAULT_WEIGHTS)

    def test_crypto_permanently_disabled_in_rotator(self):
        from agent_rotator import DISABLED_AGENTS
        self.assertIn("CryptoAgent", DISABLED_AGENTS)

    def test_improver_uses_rotator_vocab(self):
        import improver_agent
        src = inspect.getsource(improver_agent)
        self.assertIn("severity", src)
        self.assertIn("auto_actions.json", src)

    def test_scheduler_calls_improver(self):
        import market_scheduler as ms
        src = inspect.getsource(ms.run_eval_cycle)
        self.assertIn("ImproverAgent", src)
        self.assertIn("ensure_protective_exits", inspect.getsource(ms.run_agent_tick))
        self.assertIn("close_ghosts", inspect.getsource(ms.run_agent_tick))

    def test_volatility_and_movers_variants_empty_for_edge_b(self):
        from agent_rotator import AGENT_VARIANTS
        self.assertEqual(AGENT_VARIANTS.get("VolatilityAgent"), [])
        self.assertEqual(AGENT_VARIANTS.get("MoversAgent"), [])
        self.assertNotIn("MeanReversionAgent", AGENT_VARIANTS.get("VolatilityAgent") or [])
        self.assertNotIn("MomentumAgent", AGENT_VARIANTS.get("MoversAgent") or [])
        self.assertNotIn("BreakoutAgent", AGENT_VARIANTS.get("MoversAgent") or [])

    def test_recovered_sitout_is_reactivated_not_promoted(self):
        import inspect
        import agent_rotator
        src = inspect.getsource(agent_rotator.AgentRotator._reactivate_recovered)
        self.assertIn("REACTIVATED", src)
        self.assertNotIn('"PROMOTED"', src)
        self.assertNotIn("'PROMOTED'", src)

    def test_improver_does_not_auto_bench(self):
        import inspect
        import improver_agent
        src = inspect.getsource(improver_agent.ImproverAgent._write_auto_actions)
        self.assertNotIn('"BENCH"', src)
        self.assertIn("REACTIVATE", src)


class ImproverMinActiveFloor(unittest.TestCase):
    def test_improver_bench_respects_min_active_and_finds_replacement(self):
        import json
        import tempfile
        from pathlib import Path
        from agent_rotator import AgentRotator, MIN_ACTIVE_AGENTS

        self.assertEqual(MIN_ACTIVE_AGENTS, 2)
        blank = AgentRotator._blank_agent_entry()
        rot = AgentRotator.__new__(AgentRotator)
        summary = {
            "MomentumAgent": {**blank, "active": True},
            "BreakoutAgent": {**blank, "active": True},
            "TechnicalAgent": {**blank, "active": False},
        }
        payload = {
            "auto": [
                {"action": "BENCH", "agent": "MomentumAgent", "reason": "test"},
                {"action": "BENCH", "agent": "BreakoutAgent", "reason": "test"},
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "auto_actions.json"
            p.write_text(json.dumps(payload))
            events = []

            def _capture(agent_name, event_type, description, dry_run, replacement=None):
                events.append((event_type, agent_name, replacement))

            with patch("agent_rotator.AUTO_ACTIONS", p):
                with patch.object(
                    AgentRotator, "_write_rotation_event", staticmethod(_capture)
                ):
                    actions = rot._apply_improver_auto_actions(
                        report=None,
                        summary=summary,
                        newly_benched=set(),
                        dry_run=True,
                        active_count=2,
                    )
        self.assertTrue(any("SKIPPED improver bench" in a for a in actions))
        self.assertFalse(any(e[0] == "BENCHED" for e in events))
        self.assertTrue(summary["MomentumAgent"]["active"])
        self.assertTrue(summary["BreakoutAgent"]["active"])

    def test_improver_bench_above_floor_uses_find_replacement(self):
        import json
        import tempfile
        from pathlib import Path
        from agent_rotator import AgentRotator

        blank = AgentRotator._blank_agent_entry()
        rot = AgentRotator.__new__(AgentRotator)
        summary = {
            "MomentumAgent": {**blank, "active": True},
            "NewsAgent": {**blank, "active": True},
            "SentimentAgent": {**blank, "active": True},
            "BreakoutAgent": {**blank, "active": False},
        }
        payload = {
            "auto": [
                {"action": "BENCH", "agent": "MomentumAgent", "reason": "flagged"},
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "auto_actions.json"
            p.write_text(json.dumps(payload))
            events = []

            def _capture(agent_name, event_type, description, dry_run, replacement=None):
                events.append((event_type, agent_name, replacement, description))

            with patch("agent_rotator.AUTO_ACTIONS", p):
                with patch.object(
                    AgentRotator, "_write_rotation_event", staticmethod(_capture)
                ):
                    actions = rot._apply_improver_auto_actions(
                        report=None,
                        summary=summary,
                        newly_benched=set(),
                        dry_run=False,
                        active_count=3,
                    )
        self.assertFalse(summary["MomentumAgent"]["active"])
        self.assertTrue(summary["BreakoutAgent"]["active"])
        self.assertTrue(any(e[0] == "BENCHED" and e[2] == "BreakoutAgent" for e in events))
        self.assertTrue(any("PROMOTED BreakoutAgent" in a for a in actions))


if __name__ == "__main__":
    unittest.main()
