"""
ensemble.py  (replaces ensemble_v11.py — market_scheduler imports THIS file)
─────────────────────────────────────────────────────────────────────────────
Orchestrates the full 12-agent pipeline each trading tick.

Changes vs v11:
  • Alpaca real-time stream is started at first import; agents query the
    shared price cache via alpaca_stream.get_latest_price() when available,
    falling back to yfinance automatically.
  • Surge detection: after each tick, check for real-time surges/drops and
    emit opportunistic signals (fast-moving stocks caught by Alpaca streaming).
  • Strategy learner hooks: after cycle, record which agents fired and any
    outcome data available so the learner can improve thresholds over time.

Pipeline (one full cycle):
  1. RegimeDetector.detect()            → identify current market regime
  2. RiskAgent.assess()                 → get risk status / halt check
  3. All 12 agents generate signals     → raw signal list
  4. Surge scan (Alpaca)                → catch real-time breakouts/drops
  5. MetaAgent.synthesize()             → merge + weight by P&L + regime
  6. AgentRiskBridge.evaluate_signal()  → 7-gate validation
  7. PerformanceLogger                  → log all results
  8. Return approved signals for order execution
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

# Hard cap on NEW equity entries per trading day. The concentrated-risk
# mandate (2026-07-15): 3-4 big high-conviction trades a day, not a stream
# of small ones — Jul 13 alone opened 15 positions under per-tick caps,
# which is exactly the bleed pattern the clean-epoch data convicted.
DAILY_TRADE_CAP = int(os.getenv("DAILY_TRADE_CAP", "4"))

AGENT_SUMMARY_PATH = Path(__file__).resolve().parent / "logs" / "agent_summary.json"

# ── Start Alpaca streaming at import time ─────────────────────────────────────
try:
    import alpaca_stream
    alpaca_stream.start()
    _ALPACA_OK = True
except Exception as _e:
    log.warning(f"Alpaca streaming unavailable: {_e} — using yfinance only")
    _ALPACA_OK = False


def _load_benched_agent_names() -> set[str]:
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
        log.warning(f"Could not read agent_summary.json ({e}) — treating all as active")
        return set()


class Ensemble:
    """Full 12-agent ensemble. One instance per scheduler tick."""

    def __init__(self):
        from technical_agent       import TechnicalAgent
        from news_agent            import NewsAgent
        from sentiment_agent       import SentimentAgent
        from momentum_agent        import MomentumAgent
        from breakout_agent        import BreakoutAgent
        from bearish_pattern_agent import BearishPatternAgent
        from short_momentum_agent  import ShortMomentumAgent
        from earnings_agent        import EarningsAgent
        from macro_agent           import MacroAgent
        from premarket_agent       import PremarketAgent
        from sector_rotation_agent import SectorRotationAgent
        from options_flow_agent    import OptionsFlowAgent
        from volatility_agent      import VolatilityAgent
        from intermarket_agent     import IntermarketAgent
        from movers_agent          import MoversAgent
        from risk_agent            import RiskAgent
        from meta_agent            import MetaAgent
        from agent_risk_bridge     import AgentRiskBridge
        from performance_logger    import PerformanceLogger
        from regime_detector       import RegimeDetector

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
            VolatilityAgent(),
            IntermarketAgent(),
            MoversAgent(),
        ]

        self.risk   = RiskAgent()
        self.meta   = MetaAgent()
        self.bridge = AgentRiskBridge(account_balance=ACCOUNT_BALANCE)
        self.logger = PerformanceLogger()
        self.regime = RegimeDetector()

        mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
        alpaca_status = "Alpaca streaming LIVE" if (_ALPACA_OK and alpaca_stream.is_streaming()) else "yfinance only"
        log.info(f"Ensemble initialized — {mode} — {len(self.agents)} agents — {alpaca_status}")

    def run_cycle(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        log.info(f"── Ensemble cycle start {now.strftime('%H:%M:%S UTC')} ──")

        # Step 1: regime
        try:
            regimes = self.regime.detect()
        except Exception as e:
            log.warning(f"RegimeDetector failed: {e} — NEUTRAL")
            regimes = {"NEUTRAL"}

        # Step 2: risk gate
        risk_status = self.risk.assess()
        if risk_status["halt_trading"]:
            log.warning(f"TRADING HALTED: {risk_status['warnings']}")
            return []

        # Step 2a: daily entry budget — once DAILY_TRADE_CAP equity positions
        # have opened today, we're done adding risk until tomorrow.
        try:
            import trade_ledger as _tl
            from datetime import datetime as _dt
            _today = _dt.now(_tl.ET).strftime("%Y-%m-%d")
            opened_today = len([t for t in _tl.trades_on_date(_today)
                                if "/" not in t.symbol])
        except Exception:
            opened_today = 0
        entries_remaining = DAILY_TRADE_CAP - opened_today
        if entries_remaining <= 0:
            log.info(f"Daily trade cap reached ({opened_today}/{DAILY_TRADE_CAP}) "
                     f"— managing open positions only, no new entries today")
            return []

        # Step 3: gather signals from active agents
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
            log.info(f"⏸  Benched agents skipped: {', '.join(skipped)}")

        # Step 4: Alpaca surge scan — catch real-time moves
        surge_signals = self._scan_surges(risk_status)
        if surge_signals:
            log.info(f"AlpacaSurge: {len(surge_signals)} real-time signal(s)")
            all_raw_signals.extend(surge_signals)

        log.info(f"Total raw signals: {len(all_raw_signals)}")

        if not all_raw_signals:
            log.info("No raw signals this tick — conditions not met.")
            return []

        # Step 5: MetaAgent synthesis
        try:
            synthesized = self.meta.synthesize(all_raw_signals, risk_status, regimes)
        except Exception as e:
            log.error(f"MetaAgent synthesis failed: {e}", exc_info=True)
            return []

        if not synthesized:
            log.info("MetaAgent: no signals passed synthesis threshold.")
            return []

        # Step 6: risk bridge validation
        approved = []
        for signal in synthesized:
            try:
                # Normalize stop/target to the symbol's real volatility.
                # The fixed 2%-stop/5%-target geometry killed the clean
                # epoch: 39 of 49 trades stopped out on ordinary intraday
                # noise (20% win rate vs the 29% that geometry requires).
                # ATR-derived levels give volatile names room to breathe
                # and quiet names tighter, reachable targets.
                signal = self._normalize_geometry(signal)
                # Dedup gate: one open position per symbol, either side.
                # Same-side re-entry caused the duplicate-position pileup;
                # opposite-side entry fails anyway at Alpaca ("bracket orders
                # must be entry orders" — a new bracket can't open against an
                # existing position it would partially close). Block both.
                import trade_ledger as _ledger
                if (_ledger.has_open_position(signal["symbol"], "LONG")
                        or _ledger.has_open_position(signal["symbol"], "SHORT")):
                    log.info(
                        f"⏭  SKIPPED: {signal['symbol']:6} {signal['direction']:5} "
                        f"— already have an open position on this symbol"
                    )
                    continue

                if len(approved) >= entries_remaining:
                    log.info(f"⏭  Daily entry budget exhausted this tick "
                             f"({DAILY_TRADE_CAP}/day) — skipping remaining signals")
                    break

                result = self.bridge.evaluate_signal(signal)
                if result["approved"]:
                    log.info(
                        f"✅ APPROVED: {signal['symbol']:6} {signal['direction']:5} "
                        f"conf={signal['confidence']:.2f} tier={result.get('account_tier', '?')}"
                    )
                    approved.append(result)
                    from order_executor import execute_signal
                    execute_signal(result)
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
                log.error(f"Bridge eval failed for {signal.get('symbol')}: {e}", exc_info=True)

        log.info(
            f"── Cycle: {len(all_raw_signals)} raw → "
            f"{len(synthesized)} synthesized → {len(approved)} approved ──"
        )
        return approved

    def _scan_surges(self, risk_status: dict) -> list[dict]:
        """Use Alpaca real-time data to catch surges/drops ≥ 3%."""
        if not (_ALPACA_OK and alpaca_stream.is_streaming()):
            return []
        if risk_status.get("halt_trading"):
            return []

        surges = alpaca_stream.detect_surges(threshold_pct=1.5)
        signals = []
        for s in surges[:5]:  # cap at 5 surge signals per cycle
            symbol    = s["symbol"]
            pct       = s["pct_move"]
            direction = "long" if s["direction"] == "up" else "short"
            price     = s["price"]

            if direction == "long":
                stop   = round(price * 0.975, 2)
                target = round(price * 1.05,  2)
                strat  = "single_leg_calls"
            else:
                stop   = round(price * 1.025, 2)
                target = round(price * 0.95,  2)
                strat  = "single_leg_puts"

            # Lower confidence for surge signals — they need corroboration
            confidence = min(0.65 + abs(pct) * 0.02, 0.78)

            signals.append({
                "agent":           "AlpacaSurgeDetector",
                "strategy":        strat,
                "instrument_type": "options",
                "symbol":          symbol,
                "direction":       direction,
                "entry_price":     round(price, 2),
                "stop_loss_price": stop,
                "target_price":    target,
                "option_premium":  None,
                "futures_symbol":  None,
                "confidence":      round(confidence, 3),
                "expiration":      _today_expiry(),
                "meta_score":      round(confidence, 3),
                "regime_affinity": [],
                "reasons":         [f"Real-time surge {pct:+.1f}% on {s['volume']:,.0f} shares"],
                "timestamp":       datetime.now(timezone.utc).isoformat(),
            })
        return signals

    @staticmethod
    def _normalize_geometry(signal: dict) -> dict:
        """Re-derive stop/target from daily ATR(14): stop 1.5x, target 2.5x.

        Daily bars, not 5-minute — positions are held for days under GTC
        brackets, so the stop must survive a normal day's range. Breakeven
        win rate at this 1.5:2.5 geometry is 37.5%. Floor of 1% of entry
        guards against ultra-quiet symbols producing paper-thin stops.
        Falls back to the agent's own levels if data is unavailable.
        """
        symbol = signal.get("symbol", "")
        if "/" in symbol:            # crypto — its own scheduler handles it
            return signal
        try:
            import pandas as pd
            import yfinance as yf
            df = yf.Ticker(symbol).history(period="3mo", interval="1d")
            if df is None or len(df) < 20:
                return signal
            prev_close = df["Close"].shift(1)
            tr = pd.concat([
                df["High"] - df["Low"],
                (df["High"] - prev_close).abs(),
                (df["Low"] - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = float(tr.rolling(14).mean().iloc[-1])
            entry = float(signal.get("entry_price") or 0)
            if atr <= 0 or entry <= 0:
                return signal
            stop_dist = max(1.5 * atr, entry * 0.01)
            # target_price is a distant bookkeeping marker (4x stop) — the
            # real exit is the broker-side trailing stop, which is uncapped
            # on winners. Keeping the marker far out stops the ledger's
            # price simulation from fake-closing runners at +2.5 ATR.
            if signal.get("direction") == "long":
                signal["stop_loss_price"] = round(entry - stop_dist, 2)
                signal["target_price"]    = round(entry + stop_dist * 4.0, 2)
            else:
                signal["stop_loss_price"] = round(entry + stop_dist, 2)
                signal["target_price"]    = round(entry - stop_dist * 4.0, 2)
        except Exception:
            pass
        return signal

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


def _today_expiry() -> str:
    """Nearest Friday from today."""
    from datetime import timedelta
    today  = datetime.now(timezone.utc).date()
    days   = (4 - today.weekday()) % 7
    if days == 0:
        days = 7
    return (today + timedelta(days=days)).strftime("%Y-%m-%d")


_ensemble_instance: "Ensemble | None" = None

def run_ensemble() -> list[dict]:
    """
    Entry point called by market_scheduler.py each tick.
    Reuses the same Ensemble instance across ticks to avoid
    re-importing all 12 agents and re-loading weights every minute.
    """
    global _ensemble_instance
    if _ensemble_instance is None:
        _ensemble_instance = Ensemble()
    return _ensemble_instance.run_cycle()


if __name__ == "__main__":
    results = run_ensemble()
    print(f"\n{'='*60}")
    print(f"Ensemble complete: {len(results)} approved signal(s)")
    for r in results:
        print(f"\n  {r['symbol']} {r['direction'].upper()}")
        print(f"  Agent:      {r.get('agent', '?')}")
        print(f"  Confidence: {r.get('confidence', '?')}")
        print(f"  Entry:      ${r.get('entry_price', '?')}")
        print(f"  Stop:       ${r.get('stop_loss_price', '?')}")
        print(f"  Target:     ${r.get('target_price', '?')}")
