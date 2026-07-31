"""
options_executor.py
────────────────────
Defined-risk leverage: buy calls/puts instead of shares on high-conviction
signals.

Why this exists (2026-07-30): margin leverage nearly ruined the account —
running 2.43x on equities turned a -2.5% SPY day into -15%, because a
share position's downside is unbounded and gaps blow through stops
(AMKR gapped -14.5% overnight and lost 4x its intended risk). A long
option's maximum loss is the premium paid. Full stop. No margin call, no
gap risk, no overnight tail. That is the only form of leverage that
belongs in this account until expectancy is proven.

Trade-offs this ACCEPTS (they are real, not hidden):
  • A losing option often goes to ZERO — 100% loss is normal, where a
    stopped-out share position loses ~4%. Sizing must reflect that.
  • Theta: the position bleeds value every day even if price is flat.
    Mitigated by 30-45 DTE entries and closing at <=10 DTE.
  • Spreads are wider than stocks — enforced via a max-spread filter.

Risk model:
  • Premium paid per trade IS the max loss, capped at OPTIONS_RISK_PCT of
    equity (default 1% ~= $900). This is the "only risk our principal"
    property the equity book never had.
  • Only signals at or above OPTIONS_MIN_CONFIDENCE route here; everything
    else still trades shares. Options are the conviction expression, not
    the default.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OptionsExecutor")

OPTIONS_ENABLED        = os.getenv("OPTIONS_ENABLED", "true").lower() == "true"
OPTIONS_MIN_CONFIDENCE = float(os.getenv("OPTIONS_MIN_CONFIDENCE", "0.70"))
OPTIONS_RISK_PCT       = float(os.getenv("OPTIONS_RISK_PCT", "1.0"))   # % of equity per trade
MIN_DTE, MAX_DTE       = 25, 50      # entry window: enough time for the thesis
CLOSE_DTE              = 10          # exit before theta accelerates
MAX_SPREAD_PCT         = 15.0        # skip illiquid contracts
PROFIT_TAKE_MULT       = 2.0         # close at +100% premium
STOP_LOSS_MULT         = 0.50        # close at -50% premium


def _clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    k, s = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", "")
    if not k or not s:
        return None, None
    return (TradingClient(api_key=k, secret_key=s, paper=True),
            OptionHistoricalDataClient(api_key=k, secret_key=s))


def _quote(data_client, symbol: str):
    """Return (bid, ask, mid, spread_pct) for an option symbol."""
    from alpaca.data.requests import OptionLatestQuoteRequest
    q = data_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return bid, ask, mid, (ask - bid) / mid * 100


def select_contract(client, data_client, symbol: str, direction: str,
                    underlying_price: float):
    """Pick a liquid, near-the-money contract 25-50 days out.

    Near-the-money balances leverage against probability: deep OTM is a
    lottery ticket, deep ITM costs nearly as much as the stock.
    """
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType, AssetStatus

    ctype = ContractType.CALL if direction == "long" else ContractType.PUT
    try:
        res = client.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            type=ctype,
            expiration_date_gte=date.today() + timedelta(days=MIN_DTE),
            expiration_date_lte=date.today() + timedelta(days=MAX_DTE),
            strike_price_gte=str(round(underlying_price * 0.90, 2)),
            strike_price_lte=str(round(underlying_price * 1.10, 2)),
            limit=100,
        ))
    except Exception as e:
        log.warning(f"options: contract lookup failed for {symbol}: {e}")
        return None

    contracts = list(res.option_contracts or [])
    if not contracts:
        log.info(f"options: no contracts in window for {symbol}")
        return None

    # Nearest expiry in the window, then strike closest to spot
    contracts.sort(key=lambda c: (c.expiration_date,
                                  abs(float(c.strike_price) - underlying_price)))
    for c in contracts[:6]:
        q = _quote(data_client, c.symbol)
        if not q:
            continue
        bid, ask, mid, spread = q
        if spread > MAX_SPREAD_PCT:
            continue
        return {"symbol": c.symbol, "strike": float(c.strike_price),
                "expiry": str(c.expiration_date), "mid": mid, "ask": ask,
                "spread_pct": round(spread, 1)}
    log.info(f"options: no liquid contract for {symbol} (spreads too wide)")
    return None


def execute_options_trade(signal: dict) -> dict | None:
    """Buy calls/puts for a high-conviction signal. Returns result or None
    (None means the caller should fall back to the equity path)."""
    if not OPTIONS_ENABLED:
        return None
    conf = float(signal.get("raw_confidence") or signal.get("confidence") or 0)
    try:
        from auto_tune import load as _oe_cfg
        _thr = float(_oe_cfg().get("options_min_confidence", OPTIONS_MIN_CONFIDENCE))
    except Exception:
        _thr = OPTIONS_MIN_CONFIDENCE
    if conf < _thr:
        return None
    symbol = signal.get("symbol", "")
    if not symbol or "/" in symbol:          # crypto has no options here
        return None

    client, data_client = _clients()
    if client is None:
        return None

    try:
        # DEDUP — options trades never reach trade_ledger, so the
        # ensemble's has_open_position() gate cannot see them. On
        # 2026-07-30 that let WOLF puts stack to 4 contracts across two
        # strikes (~$2,000 exposure on an $890 budget) and SOFI calls to
        # 18. The -$2,150 "single" WOLF loss was really four stacked
        # entries. Check the broker directly for any live option on this
        # underlying before adding another.
        try:
            for _p in client.get_all_positions():
                _s = str(_p.symbol)
                if len(_s) > 12 and _s.startswith(symbol):
                    log.info(f"options: {symbol} already has an open contract "
                             f"({_s}) — skipping to prevent stacking")
                    return {"status": "skipped_duplicate", "symbol": _s}
        except Exception as _de:
            log.warning(f"options dedup check failed, refusing entry: {_de}")
            return {"status": "dedup_unavailable"}

        equity = float(client.get_account().equity)
        opt_bp = float(getattr(client.get_account(), "options_buying_power", 0) or 0)
        entry  = float(signal.get("entry_price") or 0)
        if entry <= 0:
            return None

        contract = select_contract(client, data_client, symbol,
                                   signal.get("direction", "long"), entry)
        if not contract:
            return None

        # Size so the PREMIUM (the entire downside) is <= risk budget
        risk_budget = equity * (OPTIONS_RISK_PCT / 100)
        per_contract = contract["ask"] * 100
        qty = int(risk_budget / per_contract)
        if qty < 1:
            log.info(f"options: {symbol} contract ${per_contract:,.0f} exceeds "
                     f"${risk_budget:,.0f} risk budget — skipping")
            return None
        cost = qty * per_contract
        if cost > opt_bp:
            log.info(f"options: {symbol} cost ${cost:,.0f} > options BP ${opt_bp:,.0f}")
            return None

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = client.submit_order(MarketOrderRequest(
            symbol=contract["symbol"], qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        log.info(
            f"🎯 OPTIONS: {contract['symbol']} x{qty} @ ~${contract['ask']:.2f} "
            f"| cost ${cost:,.0f} = MAX LOSS | strike {contract['strike']} "
            f"exp {contract['expiry']} spread {contract['spread_pct']}% "
            f"| conf {conf:.2f} agent={signal.get('agent','?')}"
        )
        return {"status": "submitted", "instrument": "option",
                "order_id": str(order.id), "symbol": contract["symbol"],
                "underlying": symbol, "qty": qty, "premium": contract["ask"],
                "max_loss": round(cost, 2)}
    except Exception as e:
        log.warning(f"options: execution failed for {symbol}: {e}")
        return None


def manage_options_exits() -> None:
    """Exit rules for open option positions.

    Options can't use Alpaca trailing stops, so exits are managed here:
    take profit at +100%, cut at -50%, and always close before theta
    accelerates in the final days.
    """
    if not OPTIONS_ENABLED:
        return
    client, data_client = _clients()
    if client is None:
        return
    try:
        positions = [p for p in client.get_all_positions()
                     if str(getattr(p, "asset_class", "")).endswith("option")
                     or len(str(p.symbol)) > 12]
    except Exception as e:
        log.warning(f"options: position fetch failed: {e}")
        return

    for p in positions:
        sym = str(p.symbol)
        try:
            cost_basis = abs(float(p.avg_entry_price))
            cur = float(p.current_price or 0)
            if cost_basis <= 0 or cur <= 0:
                continue
            ratio = cur / cost_basis
            # Expiry embedded in OCC symbol: ROOT + YYMMDD + C/P + strike
            dte = None
            try:
                import re
                m = re.search(r"(\d{6})[CP]", sym)
                if m:
                    y, mo, d = int(m.group(1)[:2]) + 2000, int(m.group(1)[2:4]), int(m.group(1)[4:6])
                    dte = (date(y, mo, d) - date.today()).days
            except Exception:
                pass

            reason = None
            if ratio >= PROFIT_TAKE_MULT:
                reason = f"profit target +{(ratio-1)*100:.0f}%"
            elif ratio <= STOP_LOSS_MULT:
                reason = f"stop -{(1-ratio)*100:.0f}%"
            elif dte is not None and dte <= CLOSE_DTE:
                reason = f"{dte}d to expiry — theta guard"
            if not reason:
                continue

            client.close_position(sym)
            log.info(f"🎯 OPTIONS EXIT {sym}: {reason} "
                     f"(entry ${cost_basis:.2f} now ${cur:.2f}, P&L {float(p.unrealized_pl):+,.0f})")
        except Exception as e:
            log.warning(f"options: exit check failed for {sym}: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c, d = _clients()
    if c:
        a = c.get_account()
        print(f"equity ${float(a.equity):,.0f} | options BP "
              f"${float(getattr(a,'options_buying_power',0) or 0):,.0f} | "
              f"level {getattr(a,'options_trading_level','?')}")
        for sym, px, dr in (("AMD", 512.0, "long"), ("SPY", 750.0, "short")):
            ct = select_contract(c, d, sym, dr, px)
            print(f"{sym} {dr}: {ct}")
