"""
agent_evaluator.py — v2.0 (2026-04-24)
──────────────────────────────────────
Ranks agents by NET P&L using the ledger as single source of truth.

CHANGE LOG (v2.0):
  • Replaced PerformanceLogger dependency with trade_ledger.
  • Agents are discovered dynamically from the ledger (no more stale
    hard-coded ENSEMBLE_AGENTS list — if it's traded, it's evaluated).
  • Each trade counts toward both its primary_agent and its contributors
    (consistent with trade_ledger.per_agent_attribution).
  • Output shape unchanged so agent_rotator.py works without modification.

Evaluation windows:
  • 5-day  rolling  — short-term momentum signal
  • 20-day rolling  — medium-term signal (primary rotation driver)
  • All-time        — lifetime context

An agent is flagged for rotation when its 5-day OR 20-day P&L is more
than UNDERPERFORM_THRESHOLD below the ensemble average for that window.

Usage:
  from agent_evaluator import AgentEvaluator
  ev     = AgentEvaluator()
  report = ev.evaluate()
  print(report.summary_text())
  ev.save_report(report)         # writes logs/latest_eval.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import trade_ledger as _ledger

# ── Tuning knobs ────────────────────────────────────────────────────────────
UNDERPERFORM_THRESHOLD = 0.20   # 20 % below ensemble avg triggers flag
MIN_TRADES_TO_EVALUATE  = 3     # agent needs ≥ N trades before being judged
ROTATION_COOLDOWN_DAYS  = 2     # don't rotate same agent twice within N days


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class AgentStats:
    name:         str
    pnl_5d:       float = 0.0
    pnl_20d:      float = 0.0
    pnl_alltime:  float = 0.0
    trades_5d:    int   = 0
    trades_20d:   int   = 0
    trades_total: int   = 0
    wins_total:   int   = 0
    losses_total: int   = 0
    active:       bool  = True
    flagged:      bool  = False
    flag_reason:  str   = ""

    @property
    def win_rate(self) -> Optional[float]:
        closed = self.wins_total + self.losses_total
        if closed == 0:
            return None
        return round(self.wins_total / closed, 3)

    @property
    def avg_pnl_per_trade(self) -> Optional[float]:
        if self.trades_total == 0:
            return None
        return round(self.pnl_alltime / self.trades_total, 2)


@dataclass
class EvalReport:
    generated_at:   str
    agents:         list[AgentStats]
    flagged_agents: list[str]     = field(default_factory=list)
    top_agent:      Optional[str] = None
    ensemble_avg_5d:  float = 0.0
    ensemble_avg_20d: float = 0.0

    def summary_text(self) -> str:
        lines = [
            f"=== Agent Performance Evaluation — {self.generated_at} ===",
            f"Ensemble avg P&L  │  5-day: ${self.ensemble_avg_5d:,.2f}  │  20-day: ${self.ensemble_avg_20d:,.2f}",
            "",
            f"{'Agent':<20} {'5d P&L':>10} {'20d P&L':>11} {'All-Time':>11} {'Trades':>7} {'Win%':>7} {'Status':>10}",
            "─" * 82,
        ]
        for a in sorted(self.agents, key=lambda x: x.pnl_20d, reverse=True):
            win_pct = f"{a.win_rate*100:.0f}%" if a.win_rate is not None else "—"
            status  = "⚠ FLAG" if a.flagged else ("✓ active" if a.active else "● bench")
            lines.append(
                f"{a.name:<20} {a.pnl_5d:>+10,.2f} {a.pnl_20d:>+11,.2f} "
                f"{a.pnl_alltime:>+11,.2f} {a.trades_total:>7} {win_pct:>7} {status:>10}"
            )
            if a.flag_reason:
                lines.append(f"  └─ {a.flag_reason}")
        if self.top_agent:
            lines += ["", f"🏆  Top performer (20-day): {self.top_agent}"]
        if self.flagged_agents:
            lines += ["", f"⚠   Flagged for review: {', '.join(self.flagged_agents)}"]
        return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────────────────
def _today_et_date() -> datetime:
    """Naive datetime at midnight ET today (ledger timestamps are ET strings)."""
    et_now = datetime.now(_ledger.ET)
    return et_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _trade_opened_dt(t) -> Optional[datetime]:
    """Parse a trade.opened_at_et string to naive datetime; return None if malformed."""
    try:
        return datetime.strptime(t.opened_at_et[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _pnl_for_trade(t) -> float:
    """Use realized for closed trades, unrealized for open trades."""
    return t.realized_pnl if not t.is_open else t.unrealized_pnl


def _agent_active_state() -> dict[str, dict]:
    """
    Read agent_summary.json (written by agent_rotator) so we know who is
    currently benched. If the file is missing or malformed, every agent is
    treated as active.
    """
    summary_path = _ledger.LOGS_DIR / "agent_summary.json"
    if not summary_path.exists():
        return {}
    try:
        with open(summary_path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ── Main evaluator ──────────────────────────────────────────────────────────
class AgentEvaluator:
    """Reads the ledger and produces a ranked EvalReport."""

    def evaluate(self) -> EvalReport:
        all_trades = _ledger.all_trades()
        active_state = _agent_active_state()

        # ── Window cutoffs (calendar days, ET) ────────────────────────────
        today_midnight = _today_et_date()
        cutoff_5d  = today_midnight - timedelta(days=5)
        cutoff_20d = today_midnight - timedelta(days=20)

        # ── Per-agent aggregation ─────────────────────────────────────────
        # Each trade contributes to every agent in t.all_agents (primary +
        # contributors) — same attribution rule as trade_ledger.per_agent_attribution.
        agg: dict[str, dict] = {}

        for t in all_trades:
            opened = _trade_opened_dt(t)
            pnl    = _pnl_for_trade(t)

            for agent in t.all_agents:
                d = agg.setdefault(agent, {
                    "pnl_5d":       0.0, "pnl_20d":      0.0, "pnl_alltime":  0.0,
                    "trades_5d":    0,   "trades_20d":   0,   "trades_total": 0,
                    "wins_total":   0,   "losses_total": 0,
                })

                d["pnl_alltime"]  += pnl
                d["trades_total"] += 1

                # Win/loss only counts CLOSED trades — open positions are
                # too noisy to call wins or losses yet.
                if not t.is_open:
                    if t.realized_pnl >= 0:
                        d["wins_total"] += 1
                    else:
                        d["losses_total"] += 1

                if opened is None:
                    continue
                if opened >= cutoff_5d:
                    d["pnl_5d"]    += pnl
                    d["trades_5d"] += 1
                if opened >= cutoff_20d:
                    d["pnl_20d"]    += pnl
                    d["trades_20d"] += 1

        # ── Build AgentStats list ─────────────────────────────────────────
        stats_list: list[AgentStats] = []
        for name, d in agg.items():
            active_info = active_state.get(name, {})
            st = AgentStats(
                name         = name,
                pnl_5d       = round(d["pnl_5d"],      2),
                pnl_20d      = round(d["pnl_20d"],     2),
                pnl_alltime  = round(d["pnl_alltime"], 2),
                trades_5d    = d["trades_5d"],
                trades_20d   = d["trades_20d"],
                trades_total = d["trades_total"],
                wins_total   = d["wins_total"],
                losses_total = d["losses_total"],
                active       = active_info.get("active", True),
            )
            stats_list.append(st)

        # ── Ensemble averages (active agents only, exclude MetaAgent) ─────
        # MetaAgent is the wrapper and counts every trade — including it
        # would make the average meaningless.
        active = [s for s in stats_list if s.active and s.name != "MetaAgent"]
        n = len(active) or 1
        avg_5d  = sum(s.pnl_5d  for s in active) / n
        avg_20d = sum(s.pnl_20d for s in active) / n

        # ── Flag underperformers ──────────────────────────────────────────
        flagged: list[str] = []
        for st in active:
            if st.trades_20d < MIN_TRADES_TO_EVALUATE:
                continue
            reasons = []

            # 5-day window
            #   • If avg is positive: flag if agent < avg * (1 - threshold)
            #   • If avg is negative: flag if agent < avg * (1 + threshold)  (i.e. MORE negative)
            #   • If avg is zero: skip (no meaningful comparison)
            if avg_5d > 0:
                if st.pnl_5d < avg_5d * (1 - UNDERPERFORM_THRESHOLD):
                    reasons.append(
                        f"5-day P&L ${st.pnl_5d:+,.2f} is >20% below ensemble avg ${avg_5d:+,.2f}"
                    )
            elif avg_5d < 0:
                if st.pnl_5d < avg_5d * (1 + UNDERPERFORM_THRESHOLD):
                    reasons.append(
                        f"5-day P&L ${st.pnl_5d:+,.2f} is >20% worse than ensemble avg ${avg_5d:+,.2f}"
                    )

            if avg_20d > 0:
                if st.pnl_20d < avg_20d * (1 - UNDERPERFORM_THRESHOLD):
                    reasons.append(
                        f"20-day P&L ${st.pnl_20d:+,.2f} is >20% below ensemble avg ${avg_20d:+,.2f}"
                    )
            elif avg_20d < 0:
                if st.pnl_20d < avg_20d * (1 + UNDERPERFORM_THRESHOLD):
                    reasons.append(
                        f"20-day P&L ${st.pnl_20d:+,.2f} is >20% worse than ensemble avg ${avg_20d:+,.2f}"
                    )

            if reasons:
                st.flagged     = True
                st.flag_reason = " | ".join(reasons)
                flagged.append(st.name)

        # ── Top performer by 20-day P&L (excluding MetaAgent) ─────────────
        ranked = sorted(
            [s for s in active if s.trades_20d >= MIN_TRADES_TO_EVALUATE],
            key=lambda x: x.pnl_20d, reverse=True,
        )
        top = ranked[0].name if ranked else None

        return EvalReport(
            generated_at     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            agents           = stats_list,
            flagged_agents   = flagged,
            top_agent        = top,
            ensemble_avg_5d  = round(avg_5d,  2),
            ensemble_avg_20d = round(avg_20d, 2),
        )

    def save_report(self, report: EvalReport) -> Path:
        """Save the latest eval report as JSON for downstream tools to read."""
        report_path = _ledger.LOGS_DIR / "latest_eval.json"
        data = {
            "generated_at":     report.generated_at,
            "top_agent":        report.top_agent,
            "flagged_agents":   report.flagged_agents,
            "ensemble_avg_5d":  report.ensemble_avg_5d,
            "ensemble_avg_20d": report.ensemble_avg_20d,
            "agents": [
                {
                    "name":          a.name,
                    "pnl_5d":        a.pnl_5d,
                    "pnl_20d":       a.pnl_20d,
                    "pnl_alltime":   a.pnl_alltime,
                    "trades_5d":     a.trades_5d,
                    "trades_20d":    a.trades_20d,
                    "trades_total":  a.trades_total,
                    "wins_total":    a.wins_total,
                    "losses_total":  a.losses_total,
                    "win_rate":      a.win_rate,
                    "active":        a.active,
                    "flagged":       a.flagged,
                    "flag_reason":   a.flag_reason,
                }
                for a in report.agents
            ],
        }
        with open(report_path, "w") as f:
            json.dump(data, f, indent=2)
        return report_path


# ── CLI usage ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ev     = AgentEvaluator()
    report = ev.evaluate()
    print(report.summary_text())
    path = ev.save_report(report)
    print(f"\nReport saved → {path}")
