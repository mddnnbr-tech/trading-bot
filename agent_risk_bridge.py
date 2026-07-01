"""
agent_risk_bridge.py
────────────────────
Per-signal risk validator. The final gate between a synthesized signal
and order execution (paper or live).

Responsibilities:
  1. Validate signal has all required fields
  2. Enforce minimum confidence threshold
  3. Calculate position size as % of account balance
  4. Enforce MAX_POSITION_SIZE_PCT from .env
  5. Apply PDT (Pattern Day Trader) guardrails for accounts < $25k
  6. Return an approved result dict or a rejected result with a reason

Used by ensemble.py:
  bridge = AgentRiskBridge(account_balance=ACCOUNT_BALANCE)
  result = bridge.evaluate_signal(signal)
  if result["approved"]:
      log_paper_trade(result)

.env keys consumed:
  MAX_POSITION_SIZE_PCT    default 1.5   (% of account per trade)
  ACCOUNT_BALANCE          default 16000
  PAPER_TRADING            default true
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("AgentRiskBridge")

# ── Config ────────────────────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = float(os.getenv("MAX_POSITION_SIZE_PCT", "2.0"))
PAPER_TRADING         = os.getenv("PAPER_TRADING", "true").lower() == "true"
PDT_THRESHOLD         = 25_000.0    # SEC rule: accounts < $25k have PDT limits
MIN_CONFIDENCE        = 0.50        # lowered from 0.55 — match MetaAgent threshold
MAX_OPTION_PREMIUM    = 5.00        # default max option premium (per contract) if not provided

# Required fields every signal must carry
REQUIRED_SIGNAL_FIELDS = {
    "symbol", "direction", "confidence", "entry_price",
    "stop_loss_price", "target_price", "agent",
}


class AgentRiskBridge:
    """
    Evaluates a single signal from the MetaAgent and decides:
      - Is the signal valid and confident enough?
      - How large should the position be?
      - Does it comply with PDT rules?

    Returns a result dict. On approval, all original signal fields are
    included plus bridge-computed fields. On rejection, only the
    rejection_reason is included (no position is sized).
    """

    def __init__(self, account_balance: float | None = None):
        self.account_balance = account_balance or float(
            os.getenv("ACCOUNT_BALANCE", "100000")
        )
        self._pdt_trades_today: int = 0   # incremented by caller if needed

        tier = "standard" if self.account_balance >= PDT_THRESHOLD else "small"
        log.info(
            f"AgentRiskBridge initialized | balance=${self.account_balance:,.0f} "
            f"| tier={tier} | max_position={MAX_POSITION_SIZE_PCT}%"
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate_signal(self, signal: dict) -> dict:
        """
        Validate and size a single signal.

        Returns dict with at minimum:
          approved          bool
          rejection_reason  str (empty string when approved)
          account_tier      str  "small" | "standard"
          position_sizing   dict | None

        When approved, the full signal is merged in as well.
        """
        account_tier = "standard" if self.account_balance >= PDT_THRESHOLD else "small"

        # ── Step 1: Field validation ────────────────────────────────────────
        missing = REQUIRED_SIGNAL_FIELDS - set(signal.keys())
        if missing:
            return self._reject(signal, account_tier, f"Missing required fields: {missing}")

        symbol     = signal["symbol"]
        direction  = signal["direction"]
        confidence = signal.get("confidence", 0.0)
        entry      = signal.get("entry_price", 0.0)
        stop       = signal.get("stop_loss_price", 0.0)
        target     = signal.get("target_price", 0.0)

        # ── Step 2: Confidence gate ─────────────────────────────────────────
        if confidence < MIN_CONFIDENCE:
            return self._reject(
                signal, account_tier,
                f"Confidence {confidence:.2f} below minimum {MIN_CONFIDENCE}"
            )

        # ── Step 3: Price sanity ────────────────────────────────────────────
        if entry <= 0:
            return self._reject(signal, account_tier, f"Invalid entry price: {entry}")

        if direction == "long" and (stop >= entry or target <= entry):
            return self._reject(
                signal, account_tier,
                f"Price levels invalid for LONG: entry={entry} stop={stop} target={target}"
            )

        if direction == "short" and (stop <= entry or target >= entry):
            return self._reject(
                signal, account_tier,
                f"Price levels invalid for SHORT: entry={entry} stop={stop} target={target}"
            )

        # ── Step 4: Position sizing ─────────────────────────────────────────
        sizing = self._compute_position_size(signal, account_tier)
        if sizing is None:
            return self._reject(
                signal, account_tier,
                f"Position size would exceed {MAX_POSITION_SIZE_PCT}% account limit"
            )

        # ── Step 5: PDT guardrail (small accounts only) ─────────────────────
        if account_tier == "small" and not PAPER_TRADING:
            # In live mode, warn when approaching PDT limit.
            # Paper trading is always allowed regardless of PDT.
            if self._pdt_trades_today >= 3:
                return self._reject(
                    signal, account_tier,
                    "PDT limit: 3 day-trades already placed this week (account < $25k)"
                )

        # ── Approved ────────────────────────────────────────────────────────
        result = {
            **signal,
            "approved":         True,
            "rejection_reason": "",
            "account_tier":     account_tier,
            "position_sizing":  sizing,
            "bridge_timestamp": datetime.now(timezone.utc).isoformat(),
        }

        mode = "PAPER" if PAPER_TRADING else "LIVE"
        log.info(
            f"[{mode}] APPROVED: {symbol} {direction.upper()} "
            f"conf={confidence:.2f} size={sizing['contracts']} contracts "
            f"risk=${sizing['risk_amount']:.0f} tier={account_tier}"
        )
        return result

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _compute_position_size(self, signal: dict, account_tier: str) -> dict | None:
        """
        Size the position as a % of account balance, scaled by confidence.

        For options:
          - Dollar risk = account_balance * MAX_POSITION_SIZE_PCT% * confidence_scale
          - Contracts   = dollar_risk / (option_premium * 100)
          - If option_premium not available, use stop-distance as a proxy.

        Returns a sizing dict, or None if the position would violate risk limits.
        """
        confidence      = signal.get("confidence", 0.0)
        entry           = signal.get("entry_price", 0.0)
        stop            = signal.get("stop_loss_price", entry)
        instrument_type = signal.get("instrument_type", "options")
        option_premium  = signal.get("option_premium")

        # Scale allocation: full MAX_POSITION_SIZE_PCT at confidence 0.9,
        # proportionally less at lower confidence
        confidence_scale = min(confidence / 0.90, 1.0)
        max_dollar_risk  = self.account_balance * (MAX_POSITION_SIZE_PCT / 100)
        dollar_risk      = round(max_dollar_risk * confidence_scale, 2)

        if instrument_type == "options":
            # Use option_premium if available; otherwise estimate from stop distance
            if option_premium and option_premium > 0:
                premium = option_premium
            else:
                # Rough proxy: stop-distance as % of entry price × $3 base premium
                stop_dist_pct = abs(entry - stop) / entry if entry > 0 else 0.02
                premium = max(round(stop_dist_pct * entry * 0.40, 2), 0.50)
                premium = min(premium, MAX_OPTION_PREMIUM)

            # Each options contract = 100 shares
            contracts = max(int(dollar_risk / (premium * 100)), 1)
            total_cost = contracts * premium * 100

            # Enforce hard position size cap
            if total_cost > self.account_balance * (MAX_POSITION_SIZE_PCT / 100) * 2:
                return None

            return {
                "contracts":    contracts,
                "premium":      round(premium, 2),
                "total_cost":   round(total_cost, 2),
                "risk_amount":  dollar_risk,
                "risk_pct":     round((dollar_risk / self.account_balance) * 100, 2),
                "instrument":   "options",
                "account_tier": account_tier,
            }

        else:
            # Equity/crypto fallback: share-based sizing.
            #
            # Bug fixed here: the old code sized shares purely off risk
            # (dollar_risk / stop_distance) then rejected if the resulting
            # NOTIONAL exceeded a cap derived from the RISK amount — those
            # are different quantities. For a tight stop (crypto commonly
            # uses 3%), risk-based sizing legitimately produces notional
            # exposure ~33x the risked dollars (1/stop_pct) — that's normal,
            # not oversized. The old cap rejected every single crypto trade.
            # Fix: size to the SMALLER of (risk-based shares) or (a direct
            # notional cap of MAX_POSITION_SIZE_PCT of account), instead of
            # comparing notional against a risk-derived number.
            stop_distance = abs(entry - stop) if stop and stop != entry else entry * 0.02
            if stop_distance <= 0 or entry <= 0:
                return None

            shares_by_risk    = dollar_risk / stop_distance
            max_notional      = self.account_balance * (MAX_POSITION_SIZE_PCT / 100)
            shares_by_notional = max_notional / entry

            shares = int(min(shares_by_risk, shares_by_notional))
            if shares < 1:
                return None

            total_cost   = shares * entry
            actual_risk  = shares * stop_distance   # real $ at risk given the sizing that was actually used

            return {
                "shares":       shares,
                "total_cost":   round(total_cost, 2),
                "risk_amount":  round(actual_risk, 2),
                "risk_pct":     round((actual_risk / self.account_balance) * 100, 2),
                "instrument":   "equity",
                "account_tier": account_tier,
            }

    @staticmethod
    def _reject(signal: dict, account_tier: str, reason: str) -> dict:
        symbol    = signal.get("symbol", "?")
        direction = signal.get("direction", "?")
        log.info(f"REJECTED: {symbol} {direction.upper()} — {reason}")
        return {
            "approved":         False,
            "rejection_reason": reason,
            "account_tier":     account_tier,
            "position_sizing":  None,
            "symbol":           symbol,
            "direction":        direction,
            "agent":            signal.get("agent", "Unknown"),
            "bridge_timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ── Quick test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bridge = AgentRiskBridge(account_balance=16000)

    # Test 1: valid long signal
    signal_ok = {
        "agent":           "TechnicalAgent",
        "symbol":          "SPY",
        "direction":       "long",
        "confidence":      0.72,
        "entry_price":     510.00,
        "stop_loss_price": 499.80,
        "target_price":    530.40,
        "strategy":        "single_leg_calls",
        "instrument_type": "options",
        "option_premium":  None,
        "futures_symbol":  None,
        "expiration":      "2026-04-25",
        "meta_score":      0.72,
        "reasons":         ["RSI oversold", "MACD crossover"],
    }

    r = bridge.evaluate_signal(signal_ok)
    print(f"\n[Test 1] SPY LONG  → approved={r['approved']}")
    if r["approved"]:
        ps = r["position_sizing"]
        print(f"  Contracts: {ps['contracts']}  Premium: ${ps['premium']}  "
              f"Risk: ${ps['risk_amount']:.0f} ({ps['risk_pct']}%)")

    # Test 2: low confidence — should reject
    signal_low_conf = {**signal_ok, "symbol": "TSLA", "confidence": 0.40}
    r2 = bridge.evaluate_signal(signal_low_conf)
    print(f"\n[Test 2] TSLA low conf → approved={r2['approved']}  reason='{r2['rejection_reason']}'")

    # Test 3: bad price levels — should reject
    signal_bad = {**signal_ok, "symbol": "AMD", "stop_loss_price": 520.0}  # stop above entry for long
    r3 = bridge.evaluate_signal(signal_bad)
    print(f"\n[Test 3] AMD bad levels → approved={r3['approved']}  reason='{r3['rejection_reason']}'")
