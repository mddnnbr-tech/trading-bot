"""
meta_agent.py — v1.1 (2026-04-24)
─────────────────────────────────
Portfolio manager for the 12-agent ensemble. Does three jobs:

1. REGIME WEIGHTING — asks RegimeDetector what market conditions look like,
   then boosts agents that specialize in those conditions.

2. PROFIT WEIGHTING — agents that have made more money get more say.
   Uses a power curve so top earners are rewarded aggressively.

3. SYNTHESIS — merges agreeing signals, resolves conflicts by keeping
   the dominant direction, filters by confidence, and returns top signals.

Agent Roster (12 signal agents + RiskAgent monitor):
  Upswing:    TechnicalAgent, MomentumAgent, BreakoutAgent
  Downswing:  TechnicalAgent (short), BearishPatternAgent, ShortMomentumAgent
  Catalyst:   EarningsAgent, MacroAgent
  Flow:       NewsAgent, SentimentAgent, OptionsFlowAgent
  Timing:     PremarketAgent, SectorRotationAgent

The MetaAgent actively promotes agents that are performing and are in
their best regime — like a coach putting their best players on the field.

CHANGE LOG (v1.1):
  • _load_performance_weights() now reads from trade_ledger.py instead of
    PerformanceLogger (which was always empty in production, so every
    agent silently fell back to DEFAULT_WEIGHTS=1.0 — the soft layer of
    the two-layer defense was inert).
  • P&L attribution counts BOTH primary_agent AND contributors, matching
    agent_evaluator v2 so weighting and rotation use the same source of
    truth.
  • Open positions (unrealized P&L) now influence weights — if an agent
    is bleeding right now, MetaAgent doesn't wait for the trade to close
    before muting it.
  • Negative-P&L agents collapse to MIN_AGENT_WEIGHT (0.15). Whole-
    ensemble-underwater windows collapse everyone to the floor (so we
    stop amplifying any agent until somebody recovers).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Set

log = logging.getLogger("MetaAgent")

# ── Synthesis config ────────────────────────────────────────────────────────
CONSENSUS_THRESHOLD    = 0.55   # raised from 0.45 — clean-epoch data showed volume
                                # of low-conviction trades was pure bleed (20% WR)
CONFLICT_CANCEL        = False  # keep dominant direction instead of cancelling
AGREEMENT_BONUS        = 0.10   # raised: extra boost when multiple agents agree
MAX_SIGNALS_PER_TICK   = 2      # top-2 per tick; combined with the ensemble's
                                # 4/day cap, entries spread across the day's best
                                # setups instead of all filling at the open
MIN_AGENT_WEIGHT       = 0.40   # lowered from 0.65 — losing agents must actually
                                # lose influence, or the weighting layer teaches nothing
MIN_SOLO_CONFIDENCE    = 0.65   # calibrated 2026-07-17: the catalyst specialists
                                # (Intermarket/Volatility) scale 0.55-0.80 and only
                                # exceed 0.70 on extreme moves — a +2.3% WTI day
                                # scored 0.68 and was blocked by the old 0.70 bar,
                                # which was never calibrated to their scale. 0.65
                                # admits real catalyst signals; 0.62 technical
                                # single-indicator noise stays blocked.
PROFIT_WEIGHT_EXPONENT = 0.6    # power curve — top earners rewarded more
REGIME_BOOST           = 0.25   # weight bonus for agents in their best regime
MIN_TRADES_FOR_WEIGHTING = 20   # don't apply P&L weighting until we have real data

# Full 12-agent roster with default weights
DEFAULT_WEIGHTS = {
    "TechnicalAgent":      1.0,
    "NewsAgent":           1.0,
    "SentimentAgent":      1.0,
    "MomentumAgent":       1.0,
    "BreakoutAgent":       1.0,
    "BearishPatternAgent": 1.0,
    "ShortMomentumAgent":  1.0,
    "EarningsAgent":       1.0,
    "MacroAgent":          1.0,
    "PremarketAgent":      1.0,
    "SectorRotationAgent": 1.0,
    "OptionsFlowAgent":    1.0,
    "VolatilityAgent":     1.0,
    "IntermarketAgent":    1.0,
}
# Note: RiskAgent is a monitor only — not a signal source


class MetaAgent:
    """
    Reads raw signals from all agents, applies risk status, weights by
    performance, deduplicates, and returns the final approved signal list.
    """

    name = "MetaAgent"

    def __init__(self):
        self.weights = self._load_performance_weights()

    def synthesize(
        self,
        all_signals: list[dict],
        risk_status: dict,
        regimes: Set[str] | None = None,
    ) -> list[dict]:
        """
        Main entry point.

        Args:
            all_signals:  Combined list of raw signals from all agents
            risk_status:  Output of RiskAgent.assess()
            regimes:      Set of active regime strings from RegimeDetector

        Returns:
            List of enriched signals ready for AgentRiskBridge
        """
        if risk_status.get("halt_trading"):
            log.warning("MetaAgent: RiskAgent has halted trading — returning no signals")
            return []

        conf_mult   = risk_status.get("confidence_multiplier", 1.0)
        active_regs = regimes or {"NEUTRAL"}

        log.info(f"MetaAgent: synthesizing {len(all_signals)} signals "
                 f"| regimes={active_regs} | conf_mult={conf_mult:.2f}")

        # Step 1: Apply risk multiplier, P&L weights, and regime boosts
        weighted = self._apply_weights(all_signals, conf_mult, active_regs)

        # Step 2: Group by (symbol, direction) and merge agreements
        merged = self._merge_signals(weighted)

        # Step 2b: Every trade must earn its way in. Clean-epoch data
        # (49 closed trades, 20% win rate, ALL agents negative) showed the
        # bleed came from volume of solo low-conviction signals. New rule:
        # a signal passes only with 2+ agreeing agents, OR a single agent
        # at >= MIN_SOLO_CONFIDENCE conviction. Shorts stay stricter —
        # always 2+ agents (solo shorts have lost in every era).
        kept = []
        for s in merged:
            n_agents = s.get("agent_count", 1)
            if s["direction"] == "short" and n_agents < 2:
                log.info(f"MetaAgent: dropped solo short {s['symbol']} — "
                         f"shorts require 2+ agent consensus")
                continue
            # Solo conviction is judged on the agent's RAW confidence, not
            # the performance-weighted one. Judging post-weight created a
            # death spiral for solo specialists (Intermarket/Volatility/
            # Surge): a negative record crushed the weight, the weighted
            # conf could never clear the bar, so the agent could never
            # trade again to repair its record — WTI +2.7% on 2026-07-17
            # fired textbook CVX/XOM signals every tick and all were
            # dropped at 0.45-0.47 despite ~0.74 raw conviction.
            raw_conf = s.get("raw_confidence", s["confidence"])
            if n_agents < 2 and raw_conf < MIN_SOLO_CONFIDENCE:
                log.info(f"MetaAgent: dropped solo {s['direction']} {s['symbol']} "
                         f"(raw conf {raw_conf:.2f} < {MIN_SOLO_CONFIDENCE} solo bar)")
                continue
            kept.append(s)
        merged = kept

        # Step 2c: Catalyst alignment — fresh news PLUS live price
        # confirmation on the same symbol is the highest-value pattern in
        # the book: it's how you catch the move at +2% while it's still
        # becoming +10%. Boost it so it outranks slower setups for the
        # day's limited entry slots.
        for s in merged:
            contribs = str(s.get("contributing_agents", s.get("agent", "")))
            if "NewsAgent" in contribs and any(
                a in contribs for a in
                ("AlpacaSurgeDetector", "MomentumAgent", "PremarketAgent")
            ):
                s["confidence"] = round(min(s["confidence"] + 0.08, 0.95), 4)
                s.setdefault("reasons", []).append(
                    "catalyst alignment: fresh news + live price confirmation")
                log.info(f"MetaAgent: catalyst boost {s['symbol']} "
                         f"{s['direction']} → conf {s['confidence']:.2f}")

        # Step 3: Cancel conflicting signals on the same symbol
        if CONFLICT_CANCEL:
            merged = self._cancel_conflicts(merged)

        # Step 4: Filter by consensus threshold
        if merged:
            top = sorted(merged, key=lambda x: x["confidence"], reverse=True)[:5]
            log.info(f"MetaAgent top merged confs: {[(s['symbol'],s['direction'],round(s['confidence'],3)) for s in top]}")
        # Solo specialists that cleared the raw-conviction bar are exempt
        # from the weighted threshold too — otherwise the weight floor
        # re-blocks at this step what the solo bar just admitted.
        passed = [
            s for s in merged
            if s["confidence"] >= CONSENSUS_THRESHOLD
            or (s.get("agent_count", 1) == 1
                and s.get("raw_confidence", 0.0) >= MIN_SOLO_CONFIDENCE)
        ]

        # Step 5a: symbols we already hold can't consume top-N slots — the
        # ensemble's dedup gate skips them anyway, so they'd burn the
        # tick's entry budget on no-ops. (2026-07-17: held AAPL/AMD/GOOGL/
        # META occupied the top slots every tick while an executable CVX
        # oil-catalyst long sat at #5 and never traded.)
        try:
            import trade_ledger as _tl
            held = {t.symbol for t in _tl.open_positions()}
            before = len(passed)
            passed = [s for s in passed if s["symbol"] not in held]
            if len(passed) < before:
                log.info(f"MetaAgent: excluded {before - len(passed)} signal(s) "
                         f"on already-held symbols from entry slots")
        except Exception:
            pass

        # Step 5: Sort by confidence, take top N
        passed.sort(key=lambda x: x["confidence"], reverse=True)
        passed = passed[:MAX_SIGNALS_PER_TICK]

        # Step 6: Add meta_score and mark as synthesized
        for s in passed:
            s["meta_score"]    = s["confidence"]
            s["synthesized_by"] = self.name
            s["agent"]          = f"MetaAgent({s.get('contributing_agents', s['agent'])})"

        log.info(
            f"MetaAgent: {len(all_signals)} raw signals → "
            f"{len(passed)} passed synthesis (conf_mult={conf_mult:.2f})"
        )
        for s in passed:
            log.info(f"  ✓ {s['symbol']:6} {s['direction']:5} conf={s['confidence']:.2f}")

        return passed

    # ── Internal steps ──────────────────────────────────────────────────────

    def _apply_weights(
        self, signals: list[dict], conf_mult: float, regimes: Set[str]
    ) -> list[dict]:
        """
        Scale each signal's confidence by:
          1. Agent P&L weight (profitable agents get more say)
          2. Regime boost (agents in their best regime get +REGIME_BOOST)
          3. Risk multiplier (from RiskAgent)
        """
        result = []
        for s in signals:
            agent_name = s.get("agent", "Unknown")
            weight     = self.weights.get(agent_name, 1.0)

            # Regime boost: does this agent's specialty match current regime?
            agent_regimes = set(s.get("regime_affinity", []))
            if agent_regimes & regimes:   # intersection — agent is in its sweet spot
                weight = min(weight + REGIME_BOOST, 1.5)
                log.debug(f"MetaAgent: regime boost for {agent_name} in {agent_regimes & regimes}")

            new_conf = s["confidence"] * weight * conf_mult
            enriched = {**s, "confidence": round(new_conf, 4),
                        "raw_confidence": s["confidence"],
                        "original_agent": agent_name}
            result.append(enriched)
        return result

    @staticmethod
    def _merge_signals(signals: list[dict]) -> list[dict]:
        """
        Group signals by (symbol, direction).
        Multiple agents agreeing on the same symbol+direction → merged signal
        with averaged confidence + agreement bonus per extra agent.
        """
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for s in signals:
            key = (s["symbol"], s["direction"])
            groups[key].append(s)

        merged = []
        for (symbol, direction), group in groups.items():
            if len(group) == 1:
                merged.append(group[0])
                continue

            # Average confidence + bonus for each agreeing agent beyond the first
            avg_conf   = sum(s["confidence"] for s in group) / len(group)
            bonus      = AGREEMENT_BONUS * (len(group) - 1)
            final_conf = min(avg_conf + bonus, 0.92)   # cap at 0.92

            # Use the signal with the highest individual confidence as the base
            base = max(group, key=lambda x: x["confidence"])
            contributing = ", ".join(
                s.get("original_agent", s.get("agent", "?")) for s in group
            )

            merged.append({
                **base,
                "confidence":          round(final_conf, 4),
                "contributing_agents": contributing,
                "agent_count":         len(group),
            })

        return merged

    @staticmethod
    def _cancel_conflicts(signals: list[dict]) -> list[dict]:
        """
        If both LONG and SHORT signals exist for the same symbol,
        keep the higher-confidence direction rather than cancelling both.
        Up OR down markets should produce trades.
        """
        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for s in signals:
            by_symbol[s["symbol"]].append(s)

        result = []
        for symbol, group in by_symbol.items():
            directions = {s["direction"] for s in group}
            if len(directions) == 1:
                result.extend(group)
            else:
                # Conflict: keep the dominant direction by total confidence
                long_conf  = sum(s["confidence"] for s in group if s["direction"] == "long")
                short_conf = sum(s["confidence"] for s in group if s["direction"] == "short")
                dominant   = "long" if long_conf >= short_conf else "short"
                kept       = [s for s in group if s["direction"] == dominant]
                log.info(
                    f"MetaAgent: conflict on {symbol} — "
                    f"long({long_conf:.2f}) vs short({short_conf:.2f}), "
                    f"keeping {dominant}"
                )
                result.extend(kept)

        return result

    # ── Weight loading from performance data ────────────────────────────────

    @staticmethod
    def _load_performance_weights() -> dict:
        """
        Read agent 20-day P&L from trade_ledger and compute relative weights.

        Incentive system — agents are rewarded for TOTAL PROFIT:
          • Top earner always gets weight 1.0
          • Others scaled by (their_pnl / top_pnl) ^ PROFIT_WEIGHT_EXPONENT
          • Power curve (exponent < 1) means the gap is larger than linear:
            e.g., an agent at 50% of top P&L gets ~65% of the weight
          • Agents with negative P&L collapse to MIN_AGENT_WEIGHT (rotation
            handles outright benching; this is the continuous tilt layer)
          • If the WHOLE ensemble is underwater in the window, every agent
            collapses to MIN_AGENT_WEIGHT — we stop amplifying anyone
          • Winning-streak bonus: +0.15 weight for 3+ consecutive wins

        Data source change (v1.1, 2026-04-24): now reads from trade_ledger.py
        instead of PerformanceLogger (which was always empty in production).
        Counts BOTH primary_agent AND contributors so co-signers earn weight
        from wins they helped produce — matching agent_evaluator v2.
        """
        try:
            import trade_ledger as _ledger
            from datetime import datetime, timedelta

            cutoff_dt = datetime.now() - timedelta(days=20)
            cutoff_iso = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Epoch-filtered (2026-07-02): pre-fix trades carry duplicate-
            # amplified P&L that would mis-weight agents for weeks. This
            # also naturally re-triggers the MIN_TRADES_FOR_WEIGHTING
            # bootstrap: everyone runs at DEFAULT_WEIGHTS until 20 clean
            # closed trades exist, then earned weighting resumes.
            all_trades = _ledger.epoch_trades()
            closed = [t for t in all_trades if not t.is_open]
            if len(closed) < MIN_TRADES_FOR_WEIGHTING:
                log.info(
                    f"MetaAgent: only {len(closed)} closed trades "
                    f"(need {MIN_TRADES_FOR_WEIGHTING}) — using DEFAULT_WEIGHTS"
                )
                return dict(DEFAULT_WEIGHTS)
            if not all_trades:
                log.info("MetaAgent: ledger is empty — using DEFAULT_WEIGHTS")
                return dict(DEFAULT_WEIGHTS)

            # ── Per-agent 20-day P&L ────────────────────────────────────
            # Count BOTH primary_agent AND contributors so an agent that
            # frequently appears as a co-signer still earns weight from
            # those trades' outcomes.
            agent_pnl: dict[str, float] = {name: 0.0 for name in DEFAULT_WEIGHTS}
            for t in all_trades:
                if t.opened_at_et < cutoff_iso:
                    continue
                pnl = (t.realized_pnl or 0.0) + (t.unrealized_pnl or 0.0)
                for agent in t.all_agents:
                    if agent in agent_pnl:
                        agent_pnl[agent] += pnl

            # ── Power-curve weights ─────────────────────────────────────
            max_pnl = max(agent_pnl.values(), default=0.0)
            weights: dict[str, float] = {}

            if max_pnl <= 0:
                # Check if this is "no closed trades yet" vs "genuinely underwater"
                closed_with_pnl = [
                    t for t in all_trades
                    if t.status in ("target", "stop", "expired")
                    and (t.realized_pnl or 0.0) != 0.0
                ]
                if not closed_with_pnl:
                    log.info("MetaAgent: no closed trades with P&L yet — using DEFAULT_WEIGHTS")
                    return dict(DEFAULT_WEIGHTS)
                # Genuinely underwater — collapse everyone to floor
                log.warning(
                    "MetaAgent: no positive P&L in last 20d across signal agents "
                    f"— using MIN_AGENT_WEIGHT ({MIN_AGENT_WEIGHT}) for all"
                )
                for name in DEFAULT_WEIGHTS:
                    weights[name] = MIN_AGENT_WEIGHT
            else:
                for name, pnl in agent_pnl.items():
                    if pnl > 0:
                        raw = (pnl / max_pnl) ** PROFIT_WEIGHT_EXPONENT
                        weights[name] = max(raw, MIN_AGENT_WEIGHT)
                    else:
                        weights[name] = MIN_AGENT_WEIGHT

            # ── Winning-streak bonus (closed trades only) ──────────────
            # Sort closed trades by exit time, walk per-agent streak of
            # consecutive wins counting from most recent. 3+ → +0.15.
            closed = [t for t in all_trades
                      if t.status in ("target", "stop", "expired")]
            closed.sort(key=lambda t: t.exit_at_et or t.opened_at_et,
                        reverse=True)

            for name in DEFAULT_WEIGHTS:
                streak = 0
                for t in closed:
                    if name not in t.all_agents:
                        continue
                    if (t.realized_pnl or 0.0) > 0:
                        streak += 1
                    else:
                        break
                if streak >= 3:
                    weights[name] = min(weights.get(name, MIN_AGENT_WEIGHT) + 0.15, 1.20)
                    log.info(f"MetaAgent: {name} on {streak}-win streak → weight bonus")

            # Round for log readability
            pretty = {n: round(w, 3) for n, w in weights.items()}
            log.info(f"MetaAgent profit weights (20d, ledger-sourced): {pretty}")
            return weights

        except Exception as e:
            log.warning(
                f"MetaAgent: could not load performance weights ({e}), "
                "using DEFAULT_WEIGHTS"
            )
            return dict(DEFAULT_WEIGHTS)


# ── Quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Simulate signals from two agents agreeing on SPY
    test_signals = [
        {
            "agent": "TechnicalAgent", "symbol": "SPY", "direction": "long",
            "confidence": 0.72, "strategy": "single_leg_calls",
            "instrument_type": "options", "entry_price": 510.0,
            "stop_loss_price": 499.8, "target_price": 530.4,
            "option_premium": None, "futures_symbol": None,
            "expiration": "2026-04-25", "meta_score": 0.72,
            "reasons": ["RSI oversold", "MACD crossover"],
        },
        {
            "agent": "SentimentAgent", "symbol": "SPY", "direction": "long",
            "confidence": 0.62, "strategy": "single_leg_calls",
            "instrument_type": "options", "entry_price": 510.0,
            "stop_loss_price": 497.25, "target_price": 530.4,
            "option_premium": None, "futures_symbol": None,
            "expiration": "2026-05-02", "meta_score": 0.62,
            "reasons": ["Extreme fear F&G=18"],
        },
    ]
    risk_ok = {
        "status": "GREEN", "halt_trading": False,
        "confidence_multiplier": 1.0, "vix": 18.5,
        "daily_pnl": 0, "weekly_pnl": 0, "consecutive_losses": 0, "warnings": [],
    }

    meta    = MetaAgent()
    results = meta.synthesize(test_signals, risk_ok)
    print(f"\nMetaAgent output: {len(results)} signal(s)")
    for r in results:
        print(f"  {r['symbol']} {r['direction']} conf={r['confidence']:.3f} "
              f"contributors={r.get('contributing_agents', r['agent'])}")
