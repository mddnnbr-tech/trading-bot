"""
improver_agent.py
─────────────────
The "coach" layer above AgentRotator.

AgentRotator handles MECHANICAL rotation: bench underperformers for 3 days,
re-activate when cooldown expires, swap in known variants. It runs without
human approval because its actions are reversible and small.

ImproverAgent handles STRUCTURAL recommendations that need human judgment:
  • Permanently retire an agent that's been bleeding for weeks
  • Add a new agent variant (TechnicalAgent_v2, NewsAgent_v2, etc.)
  • Adjust ensemble-wide tuning knobs (confidence threshold, regime weights)
  • Flag systemic issues — e.g., everyone losing money simultaneously
  • Note when no closed trades have happened in N days (pipeline broken?)

It does NOT mutate agent_summary.json or any live state. Instead it writes
a timestamped markdown file to `recommendations/` for Baker to review.
That file is the only handoff. Approval/rejection happens out-of-band
(edit the DISABLED_AGENTS set in agent_rotator.py, drop new variants in,
etc.).

Cron suggestion (nightly at 9 PM ET, after the trading day is fully closed):
    0 21 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/improver_agent.py

Usage:
    python improver_agent.py            # generate today's recommendation
    python improver_agent.py --print    # also print to stdout
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agent_evaluator import AgentEvaluator
from performance_logger import PerformanceLogger, LOGS_DIR, ENSEMBLE_AGENTS

# ── Tuning ──────────────────────────────────────────────────────────────────
RETIRE_AFTER_FLAGGED_DAYS = 5    # agent flagged on N consecutive eval days → retire
RETIRE_PNL_FLOOR_20D      = -200 # 20-day P&L below this with no recovery → retire
PIPELINE_STALL_DAYS       = 3    # zero closed trades in N consecutive days → flag pipeline
SIGNAL_REJECTION_RATE     = 0.85 # if >85% of signals are rejected, flag rule tuning

# Write into analysis/ so it lands beside the post-mortem and syncs
# to OneDrive. It previously wrote to recommendations/, which was
# never surfaced anywhere.
REC_DIR        = Path(__file__).parent / "analysis"
ROTATION_LOG   = LOGS_DIR / "rotation_log.jsonl"
EVAL_HISTORY   = LOGS_DIR / "eval_history.jsonl"  # written by this agent
TRADE_LOG      = LOGS_DIR / "trade_log.jsonl"
SUMMARY_FILE   = LOGS_DIR / "agent_summary.json"


@dataclass
class Recommendation:
    severity: str            # "info" | "warn" | "action"
    title:    str
    body:     str
    tags:     list[str] = field(default_factory=list)


class ImproverAgent:
    """Generates structural recommendations from accumulated eval data."""

    def __init__(self):
        self.logger    = PerformanceLogger()
        self.evaluator = AgentEvaluator()
        REC_DIR.mkdir(parents=True, exist_ok=True)

    # ── Main entry ──────────────────────────────────────────────────────────

    def run(self) -> Path:
        """Run all checks and write a recommendation file. Returns the path."""
        report = self.evaluator.evaluate()

        # Persist this eval into history so future runs can spot patterns
        self._append_eval_history(report)

        recs: list[Recommendation] = []

        recs.extend(self._check_persistent_underperformers())
        recs.extend(self._check_pipeline_health())
        recs.extend(self._check_signal_rejection_rate())
        recs.extend(self._check_ensemble_drawdown(report))
        recs.extend(self._check_top_performer_concentration(report))

        path = self._write_markdown(report, recs)
        return path

    # ── Checks ──────────────────────────────────────────────────────────────

    def _check_persistent_underperformers(self) -> list[Recommendation]:
        """Agents flagged on N consecutive eval days deserve retirement."""
        recs = []
        history = self._load_eval_history(days=RETIRE_AFTER_FLAGGED_DAYS + 2)
        if len(history) < RETIRE_AFTER_FLAGGED_DAYS:
            return recs

        # Count flag days per agent in the most recent window
        recent = history[-RETIRE_AFTER_FLAGGED_DAYS:]
        flag_counts: Counter = Counter()
        for h in recent:
            for name in h.get("flagged_agents", []):
                flag_counts[name] += 1

        # Also check 20d P&L from the most recent eval
        latest_pnl: dict[str, float] = {
            a.get("name"): a.get("pnl_20d", 0)
            for a in history[-1].get("agents", [])
        }

        for name, count in flag_counts.items():
            if count >= RETIRE_AFTER_FLAGGED_DAYS:
                pnl_20 = latest_pnl.get(name, 0)
                pnl_str = f"${pnl_20:+,.2f}"
                recs.append(Recommendation(
                    severity="action",
                    title=f"Retire {name}",
                    body=(
                        f"`{name}` has been flagged {count} consecutive eval days "
                        f"with a 20-day P&L of {pnl_str}. The bench-and-rotate cycle "
                        f"has not produced recovery. **Recommendation:** add `{name}` "
                        f"to `DISABLED_AGENTS` in `agent_rotator.py` to retire it from "
                        f"the ensemble until a tuned variant is built."
                    ),
                    tags=["agent_retirement", name],
                ))
        return recs

    def _check_pipeline_health(self) -> list[Recommendation]:
        """Is the close path actually recording trades?

        Rewritten 2026-07-31: this read PerformanceLogger/trade_log.jsonl,
        a file nothing writes to, so it reported "No closed trades in 3
        days -- pipeline may be stalled" every single day while the ledger
        recorded closes daily. A monitor crying wolf from a dead source is
        worse than no monitor.
        """
        recs: list[Recommendation] = []
        try:
            import trade_ledger as _tl
            from datetime import datetime as _dt, timedelta as _td
            cut = (_dt.now(_tl.ET) - _td(days=3)).strftime("%Y-%m-%d")
            recent = [t for t in _tl.all_trades()
                      if not t.is_open and (t.exit_at_et or "")[:10] >= cut]
            if not recent:
                recs.append(Recommendation(
                    priority="REVIEW",
                    title="No trades closed in 3 days",
                    detail=("Ledger shows no closed positions in 72h. Either "
                            "everything open is still running, or exits are not "
                            "being recorded -- check refresh_open_positions and "
                            "the broker sync."),
                    tags=["pipeline"]))
        except Exception as e:
            recs.append(Recommendation(
                priority="REVIEW", title="Ledger unreadable",
                detail=f"trade_ledger raised: {e}", tags=["pipeline"]))
        return recs

    def _check_signal_rejection_rate(self) -> list[Recommendation]:
        """If most signals are rejected by the risk bridge, the threshold is too tight."""
        recs = []
        if not TRADE_LOG.exists():
            return recs

        approved, rejected = 0, 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        reasons: Counter = Counter()

        for line in TRADE_LOG.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                continue

            if rec.get("event") == "SIGNAL_REJECTED" or rec.get("status") == "rejected":
                rejected += 1
                reason = rec.get("reason") or rec.get("rejection_reason") or "unspecified"
                reasons[reason] += 1
            elif "gross_pnl" in rec or rec.get("status") == "approved":
                approved += 1

        total = approved + rejected
        if total == 0:
            return recs

        rate = rejected / total
        if rate >= SIGNAL_REJECTION_RATE:
            top_reasons = ", ".join(f"{r} ({n})" for r, n in reasons.most_common(3))
            recs.append(Recommendation(
                severity="warn",
                title=f"Signal rejection rate is {rate*100:.0f}% over the last 5 days",
                body=(
                    f"{rejected} of {total} signals were rejected by the risk bridge. "
                    "If this stays high the ensemble can't generate trades regardless "
                    "of agent quality. **Top reasons:** "
                    f"{top_reasons}. **Recommendation:** review `agent_risk_bridge.py` "
                    "thresholds — most likely the confidence floor (currently 65% for "
                    "Growing tier) is set higher than typical agent confidence "
                    "(~62%), so almost everything is filtered. Either raise agent "
                    "confidence (regime weighting / agreement bonuses) or lower the "
                    "tier floor to 58–60%."
                ),
                tags=["risk_bridge", "tuning"],
            ))
        return recs

    def _check_ensemble_drawdown(self, report) -> list[Recommendation]:
        """Whole-ensemble negative average → systemic problem, not agent-specific."""
        recs = []
        if report.ensemble_avg_20d < -100:
            recs.append(Recommendation(
                severity="warn",
                title=f"Ensemble 20-day average is {report.ensemble_avg_20d:+,.2f}",
                body=(
                    "The whole ensemble is bleeding, not a few agents. Rotating "
                    "individual agents won't fix this — the issue is upstream. "
                    "**Likely causes:** (1) market regime mismatch — the regime "
                    "detector is misclassifying conditions; (2) risk-tier sizing "
                    "is too aggressive for current vol; (3) data feed gaps "
                    "(yfinance 404s on SPY/QQQ would explain blind decisions). "
                    "**Recommendation:** disable live execution, run paper-only "
                    "for 2 weeks, and audit `regime_detector.py` outputs vs. "
                    "actual VIX/SPY moves."
                ),
                tags=["ensemble", "regime"],
            ))
        return recs

    def _check_top_performer_concentration(self, report) -> list[Recommendation]:
        """If one agent dominates by 3x+ vs the rest, suggest spawning a variant."""
        recs = []
        active = [a for a in report.agents if a.active and a.trades_20d > 0]
        if len(active) < 3:
            return recs

        sorted_by_20d = sorted(active, key=lambda x: x.pnl_20d, reverse=True)
        top    = sorted_by_20d[0]
        median = sorted_by_20d[len(sorted_by_20d) // 2]

        if top.pnl_20d > 0 and median.pnl_20d > 0 and top.pnl_20d >= 3 * median.pnl_20d:
            recs.append(Recommendation(
                severity="info",
                title=f"{top.name} is dominating — consider a tuned variant",
                body=(
                    f"`{top.name}` 20-day P&L of ${top.pnl_20d:+,.2f} is more than "
                    f"3× the ensemble median (${median.pnl_20d:+,.2f}). "
                    "Concentrating capital on one agent is fragile — if its edge "
                    "fades you lose your best earner overnight. **Recommendation:** "
                    f"clone `{top.name}` into a more conservative variant (tighter "
                    "stops, smaller position sizing) and add it to the ensemble. "
                    f"Add an entry to `AGENT_VARIANTS` in `agent_rotator.py` so "
                    f"the variant is available for rotation."
                ),
                tags=["variant_suggestion", top.name],
            ))
        return recs

    # ── Persistence helpers ─────────────────────────────────────────────────

    def _append_eval_history(self, report) -> None:
        """Persist a tiny summary of each daily eval so we can detect patterns."""
        snapshot = {
            "date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_at":     report.generated_at,
            "top_agent":        report.top_agent,
            "flagged_agents":   list(report.flagged_agents),
            "ensemble_avg_5d":  report.ensemble_avg_5d,
            "ensemble_avg_20d": report.ensemble_avg_20d,
            "agents": [
                {
                    "name":         a.name,
                    "pnl_5d":       a.pnl_5d,
                    "pnl_20d":      a.pnl_20d,
                    "pnl_alltime":  a.pnl_alltime,
                    "trades_20d":   a.trades_20d,
                    "active":       a.active,
                    "flagged":      a.flagged,
                }
                for a in report.agents
            ],
        }
        with open(EVAL_HISTORY, "a") as f:
            f.write(json.dumps(snapshot) + "\n")

    def _load_eval_history(self, days: int) -> list[dict]:
        """Return the last N daily eval snapshots (one per day max)."""
        if not EVAL_HISTORY.exists():
            return []
        snapshots: list[dict] = []
        for line in EVAL_HISTORY.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                snapshots.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Dedupe by date, keep latest entry per day
        by_date: dict[str, dict] = {}
        for s in snapshots:
            by_date[s.get("date", "")] = s
        ordered = [by_date[d] for d in sorted(by_date.keys()) if d]
        return ordered[-days:]

    def _write_markdown(self, report, recs: list[Recommendation]) -> Path:
        """Render today's recommendations as a markdown file."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = REC_DIR / f"recommendations_{date}.md"

        lines = [
            f"# Trading Bot — Improver Recommendations · {date}",
            "",
            f"_Generated {report.generated_at} by `improver_agent.py`._  ",
            f"This file is advisory only — no live state has been changed. "
            "Review, then apply manually if you agree.",
            "",
            "## Snapshot",
            "",
            f"- **Top agent (20d):** {report.top_agent or '—'}",
            f"- **Flagged today:** {', '.join(report.flagged_agents) if report.flagged_agents else 'none'}",
            f"- **Ensemble 5d avg:** ${report.ensemble_avg_5d:+,.2f}",
            f"- **Ensemble 20d avg:** ${report.ensemble_avg_20d:+,.2f}",
            "",
        ]

        if not recs:
            lines += [
                "## Recommendations",
                "",
                "No structural changes recommended today. The rotator is handling "
                "everything within its mandate. ✅",
                "",
            ]
        else:
            sev_order = {"action": 0, "warn": 1, "info": 2}
            recs.sort(key=lambda r: sev_order.get(r.severity, 99))

            lines += ["## Recommendations", ""]
            for r in recs:
                badge = {
                    "action": "🔴 **ACTION**",
                    "warn":   "🟡 **REVIEW**",
                    "info":   "🟢 *FYI*",
                }.get(r.severity, r.severity)
                lines += [
                    f"### {badge} — {r.title}",
                    "",
                    r.body,
                    "",
                    f"_Tags: {', '.join(r.tags)}_" if r.tags else "",
                    "",
                ]

        # Append a footer with how to apply changes
        lines += [
            "---",
            "",
            "## How to apply",
            "",
            "- **Retire an agent:** add the name to `DISABLED_AGENTS` in "
            "`agent_rotator.py`, then mark it inactive in `logs/agent_summary.json` "
            "with `\"benched_at\": \"2099-01-01T00:00:00+00:00\"`.",
            "- **Add a variant:** drop the new agent module in the project root, "
            "register it in `ensemble.py`, and add an entry to `AGENT_VARIANTS` "
            "in `agent_rotator.py` so the rotator can promote it.",
            "- **Adjust risk-bridge thresholds:** edit `agent_risk_bridge.py` "
            "(confidence floor lives there) and restart the `trading-bot` service.",
            "- **Reject a recommendation:** delete this file or just ignore it. "
            "Improver re-evaluates from scratch each day.",
            "",
        ]

        path.write_text("\n".join(lines), encoding="utf-8")
        return path


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    improver = ImproverAgent()
    out = improver.run()
    print(f"Improver recommendation written → {out}")
    if "--print" in sys.argv:
        print()
        print(out.read_text(encoding="utf-8"))
