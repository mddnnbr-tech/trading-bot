"""
performance_logger.py
─────────────────────
Logs every trade signal and outcome for each agent in the ensemble.
Performance is ranked by NET PROFIT (P&L), not win-rate.

Each completed trade is appended as a JSON line to:
  logs/trade_log.jsonl

Each agent's running summary is maintained in:
  logs/agent_summary.json

Usage (called by the main trading loop after a trade closes):
  from performance_logger import PerformanceLogger
  logger = PerformanceLogger()
  logger.log_trade(
      agent_name="TechnicalAgent",
      signal="BUY",
      symbol="AAPL",
      entry_price=175.00,
      exit_price=178.50,
      quantity=10,
      trade_type="equity",   # equity | option | futures
      entry_time=datetime(...),
      exit_time=datetime(...),
      notes="RSI oversold + MACD crossover"
  )
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOGS_DIR   = BASE_DIR / "logs"
TRADE_LOG  = LOGS_DIR / "trade_log.jsonl"
SUMMARY    = LOGS_DIR / "agent_summary.json"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Known agents in the ensemble ───────────────────────────────────────────
ENSEMBLE_AGENTS = [
    "TechnicalAgent",
    "NewsAgent",
    "SentimentAgent",
    "RiskAgent",
    "MetaAgent",
]


class PerformanceLogger:
    """Append-only trade logger with per-agent P&L tracking."""

    def __init__(self):
        self._ensure_summary_file()

    # ── Public API ─────────────────────────────────────────────────────────

    def log_trade(
        self,
        agent_name: str,
        signal: str,             # BUY | SELL | SHORT | COVER
        symbol: str,
        entry_price: float,
        exit_price: float,
        quantity: float,         # shares, contracts, or lots
        trade_type: str = "equity",
        entry_time: Optional[datetime] = None,
        exit_time: Optional[datetime] = None,
        notes: str = "",
    ) -> dict:
        """Record a completed trade and update the agent's running summary."""

        entry_time = entry_time or datetime.now(timezone.utc)
        exit_time  = exit_time  or datetime.now(timezone.utc)

        gross_pnl = self._calc_pnl(signal, entry_price, exit_price, quantity, trade_type)
        duration_min = (exit_time - entry_time).total_seconds() / 60

        record = {
            "timestamp":    exit_time.isoformat(),
            "agent":        agent_name,
            "signal":       signal,
            "symbol":       symbol,
            "trade_type":   trade_type,
            "entry_price":  round(entry_price, 4),
            "exit_price":   round(exit_price, 4),
            "quantity":     quantity,
            "gross_pnl":    round(gross_pnl, 2),
            "duration_min": round(duration_min, 1),
            "notes":        notes,
        }

        # Append to JSONL log
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Update running summary
        self._update_summary(agent_name, gross_pnl)

        return record

    def log_signal_rejected(
        self,
        agent_name: str,
        symbol: str,
        reason: str,
    ):
        """Log a signal that was rejected by the risk bridge (no P&L impact)."""
        record = {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "agent":      agent_name,
            "symbol":     symbol,
            "event":      "SIGNAL_REJECTED",
            "reason":     reason,
        }
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

    def get_summary(self) -> dict:
        """Return the current agent summary dict."""
        with open(SUMMARY) as f:
            return json.load(f)

    def get_trades(self, agent_name: Optional[str] = None, last_n_days: int = 0) -> list:
        """
        Return trade records.
        agent_name  — filter to one agent (None = all agents)
        last_n_days — 0 = all time, N = trailing N calendar days
        """
        if not TRADE_LOG.exists():
            return []

        records = []
        cutoff = None
        if last_n_days > 0:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=last_n_days)

        with open(TRADE_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "gross_pnl" not in rec:
                    continue  # skip rejected-signal entries

                if agent_name and rec.get("agent") != agent_name:
                    continue

                if cutoff:
                    ts = datetime.fromisoformat(rec["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue

                records.append(rec)

        return records

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(signal, entry, exit_, qty, trade_type) -> float:
        """
        Gross P&L before commissions.
        Options: qty = number of contracts; each contract = 100 shares.
        Futures: raw price-difference × quantity (tick sizing handled upstream).
        """
        if signal in ("BUY",):
            raw = (exit_ - entry) * qty
        elif signal in ("SELL", "SHORT"):
            raw = (entry - exit_) * qty
        else:
            raw = (exit_ - entry) * qty  # default long

        if trade_type == "option":
            raw *= 100  # standard multiplier

        return raw

    def _ensure_summary_file(self):
        if SUMMARY.exists():
            return
        summary = {
            agent: {
                "total_pnl":     0.0,
                "trade_count":   0,
                "wins":          0,
                "losses":        0,
                "active":        True,
                "last_updated":  None,
            }
            for agent in ENSEMBLE_AGENTS
        }
        with open(SUMMARY, "w") as f:
            json.dump(summary, f, indent=2)

    def _update_summary(self, agent_name: str, pnl: float):
        with open(SUMMARY) as f:
            summary = json.load(f)

        if agent_name not in summary:
            summary[agent_name] = {
                "total_pnl":    0.0,
                "trade_count":  0,
                "wins":         0,
                "losses":       0,
                "active":       True,
                "last_updated": None,
            }

        entry = summary[agent_name]
        entry["total_pnl"]    = round(entry["total_pnl"] + pnl, 2)
        entry["trade_count"] += 1
        if pnl >= 0:
            entry["wins"] += 1
        else:
            entry["losses"] += 1
        entry["last_updated"] = datetime.now(timezone.utc).isoformat()

        with open(SUMMARY, "w") as f:
            json.dump(summary, f, indent=2)


# ── Quick smoke-test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import timedelta
    logger = PerformanceLogger()

    now = datetime.now(timezone.utc)

    # Simulate a couple of trades
    r1 = logger.log_trade(
        agent_name="TechnicalAgent",
        signal="BUY",
        symbol="AAPL",
        entry_price=175.00,
        exit_price=178.50,
        quantity=10,
        entry_time=now - timedelta(hours=2),
        exit_time=now,
        notes="RSI oversold test",
    )
    print("Logged:", r1)

    r2 = logger.log_trade(
        agent_name="NewsAgent",
        signal="BUY",
        symbol="TSLA",
        entry_price=200.00,
        exit_price=195.00,
        quantity=5,
        entry_time=now - timedelta(hours=1),
        exit_time=now,
        notes="Earnings miss test",
    )
    print("Logged:", r2)

    print("\nSummary:", json.dumps(logger.get_summary(), indent=2))
