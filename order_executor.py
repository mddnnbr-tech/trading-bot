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

    # After Alpaca rejects an order (insufficient buying power, bracket
    # conflicts, etc.), don't retry that symbol/side for this long. Failed
    # orders never reach the ledger, so the dedup gate can't see them —
    # without this, one rejected signal got re-approved and re-submitted
    # every 60s tick all day (1,830 doomed submissions on 2026-07-08).
    FAILURE_COOLDOWN_SEC = 3600

    def __init__(self):
        self._failed_at: dict[tuple[str, str], float] = {}
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

        import time
        cooldown_key = (symbol, direction)
        failed_at = self._failed_at.get(cooldown_key)
        if failed_at and (time.time() - failed_at) < self.FAILURE_COOLDOWN_SEC:
            remaining = int((self.FAILURE_COOLDOWN_SEC - (time.time() - failed_at)) / 60)
            log.info(f"⏳ COOLDOWN: {symbol} {direction.upper()} — last submission "
                     f"failed, retrying in ~{remaining}m")
            return {"status": "cooldown", "symbol": symbol, "direction": direction}

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
            self._failed_at[cooldown_key] = time.time()
            return self._log_only(approved_signal)

    # ── Equity entry + trailing-stop exit ─────────────────────────────────────
    def _submit_equity_bracket(
        self, symbol: str, direction: str,
        entry: float, stop: float, target: float, pos_usd: float
    ) -> dict:
        """Market entry + GTC TRAILING stop. No fixed take-profit.

        The asymmetry mandate (2026-07-15): a fixed take-profit sold every
        winner at +3-4% and forfeited the runners — a stock that goes on
        to +10% paid the same as one that stalled at the target. The
        trailing stop cuts losers at roughly the ATR stop distance, but
        ratchets up behind the high-water mark on winners and only fires
        on a real reversal. Losses stay capped; wins are uncapped. That
        asymmetry is the entire engine of a compounding account.
        """
        qty       = max(1, int(pos_usd / entry))
        side      = OrderSide.BUY  if direction == "long" else OrderSide.SELL
        exit_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
        # Trail distance = the ATR stop distance as a percent, clamped 2-6%
        trail_pct = round(min(max(abs(entry - stop) / entry * 100, 2.0), 6.0), 2)

        entry_order = self._client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
        ))

        # Wait for the fill so the trailing stop isn't rejected for missing qty
        import time as _t
        for _ in range(10):
            o = self._client.get_order_by_id(entry_order.id)
            if str(o.status).lower().endswith("filled"):
                break
            _t.sleep(1)

        from alpaca.trading.requests import TrailingStopOrderRequest
        trail_order = self._client.submit_order(TrailingStopOrderRequest(
            symbol=symbol, qty=qty, side=exit_side,
            trail_percent=trail_pct, time_in_force=TimeInForce.GTC,
        ))
        log.info(f"🪤 TRAIL SET: {symbol} exit trails {trail_pct}% behind "
                 f"high-water mark (order {trail_order.id}) — upside uncapped")

        return {
            "status":       "submitted",
            "order_id":     str(entry_order.id),
            "symbol":       symbol,
            "direction":    direction,
            "qty":          qty,
            "entry":        entry,
            "stop":         stop,
            "target":       target,       # bookkeeping marker only — real exit is the trail
            "trail_percent": trail_pct,
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
            # Crypto orders are notional (no qty) — derive fractional shares
            # from notional/entry so the ledger can compute real P&L. With
            # shares recorded as 0, unrealized P&L multiplied by zero and
            # crypto positions were invisible to every downstream evaluator.
            qty = order_result.get("qty")
            if not qty and entry > 0:
                qty = round(float(order_result.get("notional", 0)) / entry, 8)
            qty  = qty or 0
            risk = abs(entry - stop) * qty
            _ledger.record_trade(
                symbol        = signal["symbol"],
                side          = signal.get("direction", "long"),
                entry_price   = entry,
                target_price  = target,
                stop_price    = stop,
                risk_dollar   = risk,
                shares        = qty,
                primary_agent = signal.get("agent", "MetaAgent"),
                contributors  = signal.get("contributing_agents", ""),
                order_id      = order_result.get("order_id", ""),
            )
        except Exception as e:
            log.warning(f"Could not record to trade_ledger: {e}")


def _trail_for_profit(pct_gain: float) -> float:
    """Progressive trail: the bigger the gain, the tighter the protection.

    A flat 8% trail is right for a small winner that needs room to breathe
    and badly wrong for a large one. On 2026-08-07 the book held $10,653
    unrealized with ~$4,865 of it exposed — RNG was +$4,780 with $1,827
    at risk, and JBS/SPY would have exited BELOW their current price.
    Giving back half of every winner defeats the asymmetry the trailing
    stop exists to create.

    Ratchet: room while the trade is proving itself, protection once it
    has proved itself.
    """
    if pct_gain >= 15:
        return 3.0
    if pct_gain >= 8:
        return 4.0
    if pct_gain >= 4:
        return 5.5
    return 8.0


def widen_trails_on_survivors(min_days: float = 2.0,
                             widen_to_pct: float = 8.0) -> None:
    """Give positions that survive 2 days a wider leash.

    Strongest evidence in the dataset (2026-07-31): trades held 5+ days
    won 59% at +$163 avg — the only profitable bucket — while the 1-2 day
    bucket won 23% at -$184. The difference is trades cut before they
    resolved. A position that has already survived two days has earned
    room; widening its trail is what lets it reach the 5d+ bucket where
    the money is. Winners only — a loser gets no extra rope.
    """
    ex = get_executor()
    if ex._client is None:
        return
    try:
        from alpaca.trading.requests import GetOrdersRequest, TrailingStopOrderRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
        from datetime import datetime, timezone
        import trade_ledger as _tl

        held_days = {}
        for t in _tl.open_positions():
            try:
                d = (datetime.now(_tl.ET)
                     - datetime.fromisoformat(t.opened_at_et[:19]).replace(tzinfo=_tl.ET)).days
                held_days[t.symbol.replace("/", "")] = d
            except Exception:
                continue

        for p in ex._client.get_all_positions():
            sym = str(p.symbol)
            if len(sym) > 12:                      # options handled elsewhere
                continue
            if held_days.get(sym, 0) < min_days:
                continue
            if float(p.unrealized_pl) <= 0:        # losers get no extra rope
                continue
            orders = ex._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[sym]))
            trails = [o for o in orders
                      if str(getattr(o, "order_type", "")).lower().endswith("trailing_stop")]
            if not trails:
                continue
            # Target trail is driven by how much profit there is to
            # protect, not by a fixed widen-to value.
            try:
                gain_pct = float(p.unrealized_plpc) * 100
            except Exception:
                gain_pct = 0.0
            target_pct = _trail_for_profit(gain_pct)
            cur = float(getattr(trails[0], "trail_percent", 0) or 0)
            if abs(cur - target_pct) < 0.5:
                continue
            widen_to_pct = target_pct
            # Cancel-then-submit is NOT atomic: if the submit fails, the
            # position is left with NO exit order at all. The invariant
            # check found 14 positions naked this way on 2026-08-04 —
            # unbounded downside on the entire equity book. Verify the
            # replacement exists, and restore the original protection if
            # it does not.
            import time as _t
            side = OrderSide.SELL if int(p.qty) > 0 else OrderSide.BUY
            for o in trails:
                ex._client.cancel_order_by_id(o.id)
            _t.sleep(0.5)
            try:
                ex._client.submit_order(TrailingStopOrderRequest(
                    symbol=sym, qty=abs(int(p.qty)), side=side,
                    trail_percent=widen_to_pct, time_in_force=TimeInForce.GTC))
            except Exception as _se:
                log.error(f"trail widen FAILED for {sym} ({_se}) — restoring "
                          f"original {cur:.1f}% protection")
                try:
                    ex._client.submit_order(TrailingStopOrderRequest(
                        symbol=sym, qty=abs(int(p.qty)), side=side,
                        trail_percent=max(cur, 2.0), time_in_force=TimeInForce.GTC))
                except Exception as _re:
                    log.critical(f"{sym} IS UNPROTECTED — both widen and restore "
                                 f"failed ({_re}); invariant check will flag it")
                continue
            log.info(f"🪢 TRAIL {sym} {cur:.1f}% -> {widen_to_pct:.1f}% "
                     f"(held {held_days.get(sym)}d, {gain_pct:+.1f}%, "
                     f"${float(p.unrealized_pl):+,.0f}) — protection scaled to gain")
    except Exception as e:
        log.warning(f"trail widening failed: {e}")


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
