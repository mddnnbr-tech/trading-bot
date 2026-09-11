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


if __name__ == "__main__":
    unittest.main()
