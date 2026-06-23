"""
strategy_learner.py
-------------------
Analyzes closed trade outcomes to learn which setups actually work and
auto-adjusts agent parameters stored in learned_params.json.

How it works:
  1. Read all closed trades from the ledger
  2. For each trade, look at which agents fired and what the outcome was
  3. Identify patterns: which indicator combinations / confidence ranges
     produce the best win rates and P&L
  4. Write learned adjustments to logs/learned_params.json
  5. Agents can read learned_params.json at startup to self-tune

Learning dimensions:
  - Per-agent confidence threshold (raise if too many losers, lower if missing winners)
  - Per-agent stop-loss / target percentage (widen stops if getting stopped too often)
  - Regime-specific performance (which regimes each agent thrives in)
  - Time-of-day performance (morning vs afternoon)
  - Symbol-specific win rates (avoid symbols with poor track record)

Run automatically by market_scheduler.py each Friday at 3:45 PM ET
(end-of-week learning session) or manually: python strategy_learner.py
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("StrategyLearner")

BASE_DIR    = Path(__file__).resolve().parent
LOGS_DIR    = BASE_DIR / "logs"
PARAMS_FILE = LOGS_DIR / "learned_params.json"

# Minimum trades before trusting a statistic
MIN_SAMPLE  = 5
# Target win rate — agents below this have their confidence threshold raised
TARGET_WIN_RATE = 0.52


class StrategyLearner:
    """
    Reads the trade ledger and derives parameter adjustments.
    Results are stored in learned_params.json.
    """

    def learn(self) -> dict:
        """Run a full learning cycle. Returns the learned params dict."""
        import trade_ledger as _ledger

        all_trades  = _ledger.all_trades()
        closed      = [t for t in all_trades if not t.is_open]

        if len(closed) < MIN_SAMPLE:
            log.info(f"StrategyLearner: only {len(closed)} closed trades — skipping (need ≥{MIN_SAMPLE})")
            return self._load_current()

        log.info(f"StrategyLearner: analyzing {len(closed)} closed trades...")

        params = self._load_current()

        # ── Per-agent win rate and P&L ────────────────────────────────────────
        agent_stats: dict[str, dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "by_hour": defaultdict(lambda: {"wins": 0, "losses": 0}),
            "by_symbol": defaultdict(lambda: {"wins": 0, "losses": 0}),
            "stopped_out": 0, "target_hit": 0,
        })

        for t in closed:
            for agent in t.all_agents:
                d = agent_stats[agent]
                pnl = t.realized_pnl
                d["total_pnl"] += pnl
                if pnl >= 0:
                    d["wins"] += 1
                else:
                    d["losses"] += 1

                # Track exit type
                if t.status == "stop":
                    d["stopped_out"] += 1
                elif t.status == "target":
                    d["target_hit"] += 1

                # Time-of-day (ET hour)
                try:
                    hour = int(t.opened_at_et[11:13])
                    tod  = d["by_hour"][hour]
                    if pnl >= 0: tod["wins"] += 1
                    else:        tod["losses"] += 1
                except Exception:
                    pass

                # Per-symbol
                sym = t.symbol
                s   = d["by_symbol"][sym]
                if pnl >= 0: s["wins"] += 1
                else:        s["losses"] += 1

        # ── Derive adjustments ────────────────────────────────────────────────
        agent_params: dict[str, dict] = {}

        for agent, d in agent_stats.items():
            total  = d["wins"] + d["losses"]
            if total < MIN_SAMPLE:
                continue

            win_rate = d["wins"] / total
            n_stops  = d["stopped_out"]
            n_target = d["target_hit"]

            adjustments: dict = {}

            # Confidence threshold: raise if win rate is poor
            if win_rate < TARGET_WIN_RATE - 0.10:
                # Raise threshold by 5% — agent needs stronger signals
                adjustments["confidence_threshold_delta"] = +0.05
                log.info(f"  {agent}: win rate {win_rate:.1%} — raising confidence threshold +5%")
            elif win_rate > TARGET_WIN_RATE + 0.15:
                # Lower threshold slightly — agent is too conservative
                adjustments["confidence_threshold_delta"] = -0.03
                log.info(f"  {agent}: win rate {win_rate:.1%} — lowering confidence threshold -3%")

            # Stop loss: too many stops = widen stop
            if total > 0 and n_stops / total > 0.5:
                adjustments["stop_loss_delta_pct"] = +0.005  # widen by 0.5%
                log.info(f"  {agent}: stopped out {n_stops}/{total} — widening stop +0.5%")

            # Best hours: identify peak performance hours
            best_hours = []
            for hour, hd in d["by_hour"].items():
                h_total = hd["wins"] + hd["losses"]
                if h_total >= 3:
                    h_wr = hd["wins"] / h_total
                    if h_wr >= 0.60:
                        best_hours.append(hour)
            if best_hours:
                adjustments["best_hours_et"] = sorted(best_hours)

            # Bad symbols: win rate < 30% with enough trades → avoid
            bad_symbols = []
            for sym, sd in d["by_symbol"].items():
                s_total = sd["wins"] + sd["losses"]
                if s_total >= 3 and sd["wins"] / s_total < 0.30:
                    bad_symbols.append(sym)
            if bad_symbols:
                adjustments["avoid_symbols"] = bad_symbols
                log.info(f"  {agent}: poor symbols {bad_symbols} — flagged to avoid")

            agent_params[agent] = {
                "win_rate":    round(win_rate, 3),
                "total_trades": total,
                "total_pnl":   round(d["total_pnl"], 2),
                "adjustments": adjustments,
                "updated_at":  datetime.now(timezone.utc).isoformat(),
            }

        # ── Symbol-level win rates (ensemble-wide) ────────────────────────────
        symbol_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for t in closed:
            sd = symbol_stats[t.symbol]
            sd["pnl"] += t.realized_pnl
            if t.realized_pnl >= 0:
                sd["wins"] += 1
            else:
                sd["losses"] += 1

        best_symbols  = []
        worst_symbols = []
        for sym, sd in symbol_stats.items():
            total = sd["wins"] + sd["losses"]
            if total < MIN_SAMPLE:
                continue
            wr = sd["wins"] / total
            if wr >= 0.60:
                best_symbols.append(sym)
            elif wr < 0.35:
                worst_symbols.append(sym)

        # ── Summary stats ────────────────────────────────────────────────────
        total_pnl  = sum(t.realized_pnl for t in closed)
        total_wins = sum(1 for t in closed if t.realized_pnl >= 0)
        overall_wr = total_wins / len(closed)

        params["agent_params"]    = agent_params
        params["best_symbols"]    = best_symbols
        params["worst_symbols"]   = worst_symbols
        params["overall_win_rate"] = round(overall_wr, 3)
        params["overall_pnl"]     = round(total_pnl, 2)
        params["trade_count"]     = len(closed)
        params["last_learned_at"] = datetime.now(timezone.utc).isoformat()

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(PARAMS_FILE, "w") as f:
            json.dump(params, f, indent=2)

        log.info(
            f"StrategyLearner: learning complete — "
            f"{len(closed)} trades, overall win rate {overall_wr:.1%}, "
            f"total P&L ${total_pnl:+.2f}"
        )
        log.info(f"Best symbols: {best_symbols}  |  Avoid: {worst_symbols}")
        return params

    @staticmethod
    def _load_current() -> dict:
        if PARAMS_FILE.exists():
            try:
                with open(PARAMS_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def get_agent_adjustment(agent_name: str) -> dict:
        """
        Agents call this at startup to read any learned adjustments.
        Returns {} if no adjustments exist yet.
        """
        if not PARAMS_FILE.exists():
            return {}
        try:
            with open(PARAMS_FILE) as f:
                data = json.load(f)
            return data.get("agent_params", {}).get(agent_name, {}).get("adjustments", {})
        except Exception:
            return {}

    @staticmethod
    def get_worst_symbols() -> list[str]:
        """Return symbols the learner has flagged as poor performers."""
        if not PARAMS_FILE.exists():
            return []
        try:
            with open(PARAMS_FILE) as f:
                data = json.load(f)
            return data.get("worst_symbols", [])
        except Exception:
            return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    learner = StrategyLearner()
    result  = learner.learn()
    print(f"\nLearning complete:")
    print(f"  Overall win rate: {result.get('overall_win_rate', 'N/A')}")
    print(f"  Total P&L:        ${result.get('overall_pnl', 0):+.2f}")
    print(f"  Best symbols:     {result.get('best_symbols', [])}")
    print(f"  Worst symbols:    {result.get('worst_symbols', [])}")
    for agent, ap in result.get("agent_params", {}).items():
        if ap.get("adjustments"):
            print(f"  {agent}: {ap['adjustments']}")
