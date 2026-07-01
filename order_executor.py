"""
order_executor.py
-----------------
Submits actual orders to Alpaca paper trading and records them in trade_ledger.

Supports:
  - Equity LONG:  bracket order (entry + stop-loss + take-profit in one call)
  - Equity SHORT: sell-to-open bracket order
  - Crypto LONG:  market order (Alpaca crypto doesn't support bracket orders)
  - Leveraged ETFs: treated as regular equities

Position sizing is driven by the approved_signal dict from AgentRiskBridge.

.env keys consumed:
  ALPACA_API_KEY       — paper trading API key (PA3EZ46Z9UUC)
  ALPACA_API_SECRET    — paper trading secret
  PAPER_TRADING        — must be "true" (live mode not wired yet)
  RISK_PER_TRADE       — dollar risk per trade (default $320)
  MAX_POSITION_PCT     — max % of portfolio per trade (default 2.0)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OrderExecutor")

# ── Alpaca imports (alpaca-py) ────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
    _ALPACA_OK = True
except ImportError:
    _ALPACA_OK = False
    log.warning("alpaca-py not installed — orders will be logged only, not submitted")

# ── Config ────────────────────────────────────────────────────────────────────
PAPER_TRADING     = os.getenv("PAPER_TRADING", "true").lower() == "true"
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
RISK_PER_TRADE    = float(os.getenv("RISK_PER_TRADE", "320"))
MAX_POSITION_PCT  = float(os.getenv("MAX_POSITION_PCT", "2.0"))   # % of portfolio

# Symbols Alpaca handles as crypto (use notional sizing, no bracket)
CRYPTO_SYMBOLS = {"BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LTC/USD"}

# Max portfolio allocation per single position ($100k * 2% = $2k default)
PORTFOLIO_VALUE   = float(os.getenv("ACCOUNT_BALANCE", "100000"))


class OrderExecutor:
    """
    Submits orders to Alpaca and records them in trade_ledger.
    Safe to call with PAPER_TRADING=true — all orders go to paper endpoint.
    """

    def __init__(self):
        if not _ALPACA_OK:
            self._client = None
            return
        if not ALPACA_API_KEY or not ALPACA_API_SECRET:
            log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set — cannot submit orders")
            self._client = None
            return
        self._client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_API_SECRET,
            paper=PAPER_TRADING,
        )
        log.info(f"OrderExecutor ready — paper={PAPER_TRADING}")

    # ── Public entry point ─────────────────────────────────────────────────────
    def execute(self, approved_signal: dict) -> dict:
        """
        Execute a paper trade for an approved signal.

        approved_signal keys (from AgentRiskBridge):
          symbol, direction, confidence, entry_price,
          stop_loss_price, target_price, agent, position_size_usd

        Returns a result dict with status, order_id (if submitted), and trade_id.
        """
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long").lower()
        entry     = float(approved_signal.get("entry_price", 0))
        stop      = float(approved_signal.get("stop_loss_price", 0))
        target    = float(approved_signal.get("target_price", 0))
        agent     = approved_signal.get("agent", "Unknown")
        # Prefer the actual sizing AgentRiskBridge computed (risk-based,
        # accounts for stop distance) over a flat default. Previously this
        # always fell back to a fixed 2% notional regardless of what the
        # risk bridge decided, silently ignoring its sizing math.
        sizing    = approved_signal.get("position_sizing") or {}
        pos_usd   = float(sizing.get("total_cost") or approved_signal.get(
                          "position_size_usd", PORTFOLIO_VALUE * MAX_POSITION_PCT / 100))

        if entry <= 0:
            return self._reject("entry_price is 0 or missing")
        if stop <= 0 or target <= 0:
            return self._reject("stop_loss_price or target_price missing")
        if self._client is None:
            return self._log_only(approved_signal)

        is_crypto = symbol in CRYPTO_SYMBOLS

        try:
            if is_crypto:
                result = self._submit_crypto(symbol, direction, pos_usd)
            else:
                result = self._submit_equity_bracket(
                    symbol, direction, entry, stop, target, pos_usd
                )

            log.info(
                f"✅ ORDER SUBMITTED: {symbol} {direction.upper()} "
                f"${pos_usd:.0f} | order_id={result.get('order_id')} "
                f"| agent={agent}"
            )
            self._record_ledger(approved_signal, result)
            return result

        except Exception as e:
            log.error(f"Order submission failed for {symbol}: {e}", exc_info=True)
            return self._log_only(approved_signal)

    # ── Equity bracket order ──────────────────────────────────────────────────
    def _submit_equity_bracket(
        self, symbol: str, direction: str,
        entry: float, stop: float, target: float, pos_usd: float
    ) -> dict:
        qty = max(1, int(pos_usd / entry))   # whole shares only for bracket
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL

        # Build bracket: entry is market, stop and target are limit/stop
        # alpaca-py v0.8+ uses nested request objects for bracket
        from alpaca.trading.requests import TakeProfitRequest, StopLossRequest

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
        )
        order = self._client.submit_order(req)
        return {
            "status":    "submitted",
            "order_id":  str(order.id),
            "symbol":    symbol,
            "direction": direction,
            "qty":       qty,
            "entry":     entry,
            "stop":      stop,
            "target":    target,
        }

    # ── Crypto market order ───────────────────────────────────────────────────
    def _submit_crypto(self, symbol: str, direction: str, notional: float) -> dict:
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=side,
            time_in_force=TimeInForce.GTC,
        )
        order = self._client.submit_order(req)
        return {
            "status":    "submitted",
            "order_id":  str(order.id),
            "symbol":    symbol,
            "direction": direction,
            "notional":  notional,
        }

    # ── Fallback: log only (no Alpaca connection) ─────────────────────────────
    def _log_only(self, approved_signal: dict) -> dict:
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long")
        entry     = approved_signal.get("entry_price", 0)
        target    = approved_signal.get("target_price", entry)
        stop      = approved_signal.get("stop_loss_price", entry)
        agent     = approved_signal.get("agent", "Unknown")
        log.info(
            f"📋 PAPER TRADE (log-only): {symbol} {direction.upper()} "
            f"entry=${entry} target=${target} stop=${stop} agent={agent}"
        )
        return {"status": "logged", "symbol": symbol, "direction": direction}

    def _reject(self, reason: str) -> dict:
        log.warning(f"OrderExecutor rejected: {reason}")
        return {"status": "rejected", "reason": reason}

    # ── Write to trade_ledger ─────────────────────────────────────────────────
    def _record_ledger(self, signal: dict, order_result: dict) -> None:
        try:
            import trade_ledger as _ledger
            entry  = float(signal.get("entry_price", 0))
            stop   = float(signal.get("stop_loss_price", 0))
            target = float(signal.get("target_price", 0))
            risk   = abs(entry - stop) * order_result.get("qty", 1)
            _ledger.record_trade(
                symbol        = signal["symbol"],
                side          = signal.get("direction", "long"),
                entry_price   = entry,
                target_price  = target,
                stop_price    = stop,
                risk_dollar   = risk,
                shares        = order_result.get("qty", 0),
                primary_agent = signal.get("agent", "MetaAgent"),
                contributors  = signal.get("contributing_agents", ""),
                order_id      = order_result.get("order_id", ""),
            )
        except Exception as e:
            log.warning(f"Could not record to trade_ledger: {e}")


# ── Module-level singleton ────────────────────────────────────────────────────
_executor: Optional[OrderExecutor] = None

def get_executor() -> OrderExecutor:
    global _executor
    if _executor is None:
        _executor = OrderExecutor()
    return _executor


def execute_signal(approved_signal: dict) -> dict:
    """Convenience wrapper — called by ensemble.py."""
    return get_executor().execute(approved_signal)
