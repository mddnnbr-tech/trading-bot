"""
ensemble.py — v1.1 (2026-04-24)
───────────────────────────────
Orchestrates the full 12-agent pipeline each trading tick.

CHANGE LOG (v1.1):
  • Added bench-aware agent filtering. Each cycle reads
    logs/agent_summary.json and skips any agent explicitly marked
    active=False (written there by agent_rotator.py).
  • Default behavior (file missing, agent missing from file, or any
    parse error) is to RUN the agent. We only skip when there's an
    explicit active=False, which makes this safe to deploy without
    pre-populating the summary file.

Pipeline (one full cycle):
  1. RegimeDetector.detect()            → identify current market regime
  2. RiskAgent.assess()                 → get risk status / halt check
  3. All 12 agents generate signals     → raw signal list
  4. MetaAgent.synthesize()             → merge + weight by P&L + regime
  5. AgentRiskBridge.evaluate_signal()  → 7-gate validation
  6. PerformanceLogger                  → log all results
  7. Return approved signals for order execution

Agent Roster:
  Upswing:    TechnicalAgent, MomentumAgent, BreakoutAgent
  Downswing:  BearishPatternAgent, ShortMomentumAgent
  Catalyst:   EarningsAgent, MacroAgent
  Flow:       NewsAgent, SentimentAgent, OptionsFlowAgent
  Timing:     PremarketAgent, SectorRotationAgent

Paper trading mode (PAPER_TRADING=true in .env):
  All approved signals are logged but NOT sent to the order executor.

Usage (called by market_scheduler.py each tick):
  from ensemble import run_ensemble
  approved = run_ensemble()
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("Ensemble")

PAPER_TRADING   = os.getenv("PAPER_TRADING", "true").lower() == "true"
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "16000"))

# Path to the bench/active state file written by agent_rotator.py
AGENT_SUMMARY_PATH = Path(__file__).resolve().parent / "logs" / "agent_summary.json"


def _load_benched_agent_names() -> set[str]:
    """
    Read agent_summary.json and return the set of agent names that have been
    explicitly benched (active=False). Defaults to an empty set if the file
    is missing, malformed, or unreadable — this is fail-safe: when in doubt,
    let every agent trade.
    """
    try:
        if not AGENT_SUMMARY_PATH.exists():
            return set()
        with open(AGENT_SUMMARY_PATH) as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return set()
        return {
            name for name, info in data.items()
            if isinstance(info, dict) and info.get("active", True) is False
        }
    except Exception as e:
        log.warning(
            f"Could not read {AGENT_SUMMARY_PATH.name} ({e}) — "
            "treating all agents as active."
        )
        return set()


class Ensemble:
    """Full 12-agent ensemble. One instance per scheduler tick."""

    def __init__(self):
        from technical_agent      import TechnicalAgent
        from news_agent           import NewsAgent
        from sentiment_agent      import SentimentAgent
        from momentum_agent       import MomentumAgent
        from breakout_agent       import BreakoutAgent
        from bearish_pattern_agent import BearishPatternAgent
        from short_momentum_agent import ShortMomentumAgent
        from earnings_agent       import EarningsAgent
        from macro_agent          import MacroAgent
        from premarket_agent      import PremarketAgent
        from sector_rotation_agent import SectorRotationAgent
        from options_flow_agent   import OptionsFlowAgent
        from risk_agent           import RiskAgent
        from meta_agent           import MetaAgent
        from agent_risk_bridge    import AgentRiskBridge
        from performance_logger   import PerformanceLogger
        from regime_detector      import RegimeDetector

        # ── Signal agents ──────────────────────────────────────────────
        self.agents = [
            TechnicalAgent(),
            NewsAgent(),
            SentimentAgent(),
            MomentumAgent(),
            BreakoutAgent(),
            BearishPatternAgent(),
            ShortMomentumAgent(),
            EarningsAgent(),
            MacroAgent(),
            PremarketAgent(),
            SectorRotationAgent(),
            OptionsFlowAgent(),
        ]

        # ── Infrastructure ─────────────────────────────────────────────
        self.risk     = RiskAgent()
        self.meta     = MetaAgent()
        self.bridge   = AgentRiskBridge(account_balance=ACCOUNT_BALANCE)
        self.logger   = PerformanceLogger()
        self.regime   = RegimeDetector()

        mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
        log.info(f"Ensemble initialized — {mode} — {len(self.agents)} agents active")

    def run_cycle(self) -> list[dict]:
        """
        Execute one full ensemble cycle.
        Returns list of bridge-approved signals.
        """
        now = datetime.now(timezone.utc)
        log.info(f"── Ensemble cycle start {now.strftime('%H:%M:%S UTC')} ──")

        # ── Step 1: Detect market regime ─────────────────────────────
        try:
            regimes = self.regime.detect()
        except Exception as e:
            log.warning(f"RegimeDetector failed: {e} — using NEUTRAL")
            regimes = {"NEUTRAL"}

        # ── Step 2: Risk gate ─────────────────────────────────────────
        risk_status = self.risk.assess()
        if risk_status["halt_trading"]:
            log.warning(f"TRADING HALTED: {risk_status['warnings']}")
            return []

        # ── Step 3: Gather signals from all 12 agents ─────────────────
        # Read bench list fresh each cycle so rotator updates take effect
        # immediately (no scheduler restart required).
        benched = _load_benched_agent_names()
        skipped: list[str] = []

        all_raw_signals: list[dict] = []
        for agent in self.agents:
            if agent.name in benched:
                skipped.append(agent.name)
                continue
            try:
                signals = agent.generate_signals()
                if signals:
                    log.info(f"{agent.name}: {len(signals)} signal(s)")
                all_raw_signals.extend(signals)
            except Exception as e:
                log.error(f"{agent.name} failed: {e}", exc_info=True)

        if skipped:
            log.info(f"⏸️  Benched agents skipped this cycle: {', '.join(skipped)}")
        log.info(f"Total raw signals from all agents: {len(all_raw_signals)}")

        if not all_raw_signals:
            log.info("No raw signals this tick — conditions not met across all agents.")
            return []

        # ── Step 4: MetaAgent synthesis (regime + P&L weighted) ───────
        try:
            synthesized = self.meta.synthesize(all_raw_signals, risk_status, regimes)
        except Exception as e:
            log.error(f"MetaAgent synthesis failed: {e}", exc_info=True)
            return []

        if not synthesized:
            log.info("MetaAgent: no signals passed synthesis threshold.")
            return []

        # ── Step 5: Risk bridge validation (7 gates) ──────────────────
        approved = []
        for signal in synthesized:
            try:
                result = self.bridge.evaluate_signal(signal)

                if result["approved"]:
                    log.info(
                        f"✅ APPROVED: {signal['symbol']:6} {signal['direction']:5} "
                        f"conf={signal['confidence']:.2f} "
                        f"tier={result.get('account_tier', '?')}"
                    )
                    approved.append(result)

                    if PAPER_TRADING:
                        self._log_paper_trade(result)
                    else:
                        # Phase B: wire OrderExecutor here when ready to go live
                        log.info("Live order execution — OrderExecutor not yet wired (Phase B)")

                else:
                    log.info(
                        f"⛔ REJECTED: {signal['symbol']:6} — "
                        f"{result.get('rejection_reason', 'unknown')}"
                    )
                    self.logger.log_signal_rejected(
                        agent_name=signal.get("agent", "Unknown"),
                        symbol=signal["symbol"],
                        reason=result.get("rejection_reason", "bridge_rejection"),
                    )

            except Exception as e:
                log.error(f"Bridge evaluation failed for {signal.get('symbol')}: {e}",
                          exc_info=True)

        log.info(
            f"── Cycle complete: {len(all_raw_signals)} raw → "
            f"{len(synthesized)} synthesized → {len(approved)} approved ──"
        )
        return approved

    def _log_paper_trade(self, approved_signal: dict):
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long")
        entry     = approved_signal.get("entry_price", 0)
        target    = approved_signal.get("target_price", entry)
        stop      = approved_signal.get("stop_loss_price", entry)
        agent     = approved_signal.get("agent", "MetaAgent")

        log.info(
            f"📋 PAPER TRADE: {symbol} {direction.upper()} "
            f"entry=${entry} target=${target} stop=${stop} agent={agent}"
        )


# ── Convenience entry point called by market_scheduler.py ─────────────────

def run_ensemble() -> list[dict]:
    """
    Stateless entry point. Creates a fresh Ensemble each call
    so performance weights are always current.
    """
    return Ensemble().run_cycle()


# ── Quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = run_ensemble()
    print(f"\n{'='*60}")
    print(f"Ensemble complete: {len(results)} approved signal(s)")
    for r in results:
        print(f"\n  {r['symbol']} {r['direction'].upper()}")
        print(f"  Agent:     {r.get('agent', '?')}")
        print(f"  Confidence:{r.get('confidence', '?')}")
        print(f"  Entry:     ${r.get('entry_price', '?')}")
        print(f"  Stop:      ${r.get('stop_loss_price', '?')}")
        print(f"  Target:    ${r.get('target_price', '?')}")
