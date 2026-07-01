"""
risk_agent.py
─────────────
Portfolio-level risk monitor. Runs before every ensemble cycle.

Responsibilities:
  1. Check VIX against the halt threshold from .env
  2. Check daily and weekly P&L against loss limits from .env
  3. Count consecutive losing trades
  4. Compute portfolio heat (% of capital currently at risk)
  5. Return a RiskStatus that MetaAgent uses to scale or kill signals

This agent does NOT generate trade signals itself.
It returns a RiskStatus dict that the MetaAgent reads before
deciding whether to pass signals through or reduce confidence.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yfinance as yf
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("RiskAgent")

# ── Config from .env ────────────────────────────────────────────────────────
VIX_HALT_THRESHOLD      = float(os.getenv("VIX_HALT_THRESHOLD",      "35"))
DAILY_LOSS_LIMIT_PCT    = float(os.getenv("DAILY_LOSS_LIMIT_PCT",     "3.0"))
WEEKLY_LOSS_LIMIT_PCT   = float(os.getenv("WEEKLY_LOSS_LIMIT_PCT",    "8.0"))
MAX_CONSECUTIVE_LOSSES  = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     "3"))
ACCOUNT_BALANCE         = float(os.getenv("ACCOUNT_BALANCE",          "100000"))

# ── Risk status levels ──────────────────────────────────────────────────────
STATUS_GREEN  = "GREEN"    # all clear
STATUS_YELLOW = "YELLOW"   # reduce position sizes
STATUS_RED    = "RED"      # halt all new trades


class RiskAgent:
    """
    Reads current portfolio state and returns a RiskStatus dict.
    Called by ensemble.py before each signal cycle.
    """

    name = "RiskAgent"

    def __init__(self):
        self.account_balance = ACCOUNT_BALANCE
        self._load_performance_data()

    def assess(self) -> dict:
        """
        Run all risk checks. Returns a RiskStatus dict:
        {
          "status":        "GREEN" | "YELLOW" | "RED",
          "halt_trading":  bool,
          "vix":           float,
          "daily_pnl":     float,
          "weekly_pnl":    float,
          "consecutive_losses": int,
          "warnings":      [str],
          "confidence_multiplier": float,  # 1.0 = normal, 0.5 = halved
        }
        """
        # Refresh P&L on every assessment. The ensemble reuses one RiskAgent
        # instance across all ~390 daily ticks, so loading P&L only in
        # __init__ froze the circuit breaker at the 9:30 AM snapshot — a
        # mid-day drawdown could never trip the daily/weekly loss halts.
        self._load_performance_data()

        warnings  = []
        status    = STATUS_GREEN
        halt      = False
        conf_mult = 1.0

        vix = self._get_vix()

        # ── Check 1: VIX halt ─────────────────────────────────────────────
        # NOTE: VIX at 35+ = true crisis halt. Below that we scale gently
        # so volatile markets still generate trades (shorts profit in down moves).
        if vix >= VIX_HALT_THRESHOLD:
            halt   = True
            status = STATUS_RED
            warnings.append(
                f"VIX HALT: {vix:.1f} ≥ threshold {VIX_HALT_THRESHOLD} — "
                f"all new trades suspended"
            )
        elif vix >= VIX_HALT_THRESHOLD * 0.88:
            # 30.8+ → moderate caution, 90% confidence (was 75% at 28+)
            status    = STATUS_YELLOW
            conf_mult = min(conf_mult, 0.90)
            warnings.append(f"VIX elevated ({vix:.1f}) — confidence reduced to 90%")
        elif vix >= VIX_HALT_THRESHOLD * 0.74:
            # 25.9+ → mild caution, 95% confidence — still very tradeable
            conf_mult = min(conf_mult, 0.95)
            warnings.append(f"VIX moderate ({vix:.1f}) — slight confidence reduction")

        # ── Check 2: Daily loss limit ─────────────────────────────────────
        daily_limit = self.account_balance * (DAILY_LOSS_LIMIT_PCT / 100)
        if self.daily_pnl <= -daily_limit:
            halt   = True
            status = STATUS_RED
            warnings.append(
                f"DAILY LOSS LIMIT HIT: ${self.daily_pnl:,.2f} loss exceeds "
                f"${daily_limit:,.2f} ({DAILY_LOSS_LIMIT_PCT}% of account)"
            )
        elif self.daily_pnl <= -daily_limit * 0.70:
            status    = STATUS_YELLOW
            conf_mult = min(conf_mult, 0.75)
            warnings.append(
                f"Approaching daily loss limit (${self.daily_pnl:,.2f}) — "
                f"confidence reduced"
            )

        # ── Check 3: Weekly loss limit ────────────────────────────────────
        weekly_limit = self.account_balance * (WEEKLY_LOSS_LIMIT_PCT / 100)
        if self.weekly_pnl <= -weekly_limit:
            halt   = True
            status = STATUS_RED
            warnings.append(
                f"WEEKLY LOSS LIMIT HIT: ${self.weekly_pnl:,.2f} loss exceeds "
                f"${weekly_limit:,.2f} ({WEEKLY_LOSS_LIMIT_PCT}% of account)"
            )

        # ── Check 4: Consecutive losses ───────────────────────────────────
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            status    = STATUS_YELLOW if status == STATUS_GREEN else status
            conf_mult = min(conf_mult, 0.60)
            warnings.append(
                f"{self.consecutive_losses} consecutive losing trades — "
                f"confidence reduced to 60%, reviewing strategy"
            )

        if halt:
            conf_mult = 0.0

        result = {
            "status":               status,
            "halt_trading":         halt,
            "vix":                  round(vix, 2),
            "daily_pnl":            round(self.daily_pnl, 2),
            "weekly_pnl":           round(self.weekly_pnl, 2),
            "consecutive_losses":   self.consecutive_losses,
            "warnings":             warnings,
            "confidence_multiplier": conf_mult,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
        }

        # Log warnings
        for w in warnings:
            if status == STATUS_RED:
                log.warning(f"RiskAgent [{status}]: {w}")
            else:
                log.info(f"RiskAgent [{status}]: {w}")

        if not warnings:
            log.info(f"RiskAgent: {STATUS_GREEN} — all risk checks passed "
                     f"(VIX={vix:.1f}, daily P&L=${self.daily_pnl:+,.2f})")

        return result

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_performance_data(self):
        """Read today's and this week's P&L from the live ledger (paper_trades.csv).

        v2 fix: this used to read from PerformanceLogger/trade_log.jsonl, a file
        the bot never writes to — meaning daily/weekly loss halts and the
        consecutive-loss throttle silently never fired. trade_ledger is the
        single source of truth everywhere else in the system; use it here too.
        """
        try:
            import trade_ledger as _ledger
            from datetime import timedelta

            all_trades = _ledger.all_trades()
            today_str  = datetime.now(_ledger.ET).strftime("%Y-%m-%d")
            week_cut   = (datetime.now(_ledger.ET) - timedelta(days=7)).strftime("%Y-%m-%d")

            def pnl(t):
                return t.realized_pnl if not t.is_open else t.unrealized_pnl

            trades_today = [t for t in all_trades if t.opened_at_et[:10] == today_str]
            trades_week  = [t for t in all_trades if t.opened_at_et[:10] >= week_cut]

            self.daily_pnl  = sum(pnl(t) for t in trades_today)
            self.weekly_pnl = sum(pnl(t) for t in trades_week)

            # Count consecutive losses from most recent CLOSED trades backward
            closed = sorted(
                [t for t in all_trades if not t.is_open],
                key=lambda t: t.exit_at_et or t.opened_at_et,
            )
            self.consecutive_losses = 0
            for trade in reversed(closed):
                if (trade.realized_pnl or 0.0) < 0:
                    self.consecutive_losses += 1
                else:
                    break

        except Exception as e:
            log.warning(f"Could not load performance data: {e}")
            self.daily_pnl          = 0.0
            self.weekly_pnl         = 0.0
            self.consecutive_losses = 0

    @staticmethod
    def _get_vix() -> float:
        try:
            df = yf.Ticker("^VIX").history(period="2d")
            return float(df["Close"].iloc[-1]) if not df.empty else 20.0
        except Exception:
            return 20.0


# ── Quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    agent  = RiskAgent()
    status = agent.assess()
    print(f"\nRisk Status: {status['status']}")
    print(f"  VIX:              {status['vix']}")
    print(f"  Daily P&L:        ${status['daily_pnl']:+,.2f}")
    print(f"  Weekly P&L:       ${status['weekly_pnl']:+,.2f}")
    print(f"  Consec. losses:   {status['consecutive_losses']}")
    print(f"  Conf. multiplier: {status['confidence_multiplier']}")
    if status["warnings"]:
        print("  Warnings:")
        for w in status["warnings"]:
            print(f"    ⚠  {w}")
