"""
crypto_scheduler.py
────────────────────
Runs ONE crypto trading tick and exits. Scheduled via cron every 15 min,
every day of the week — crypto trades 24/7, so unlike market_scheduler.py
this has NO equity-market-hours gate.

Deliberately a separate script from market_scheduler.py rather than
removing the equity hours gate there — mixing 24/7 crypto ticks into the
same process as the equity loop would be exactly the kind of unreviewed
architecture change that caused the duplicate-position bug. Keeping it
isolated means a crypto bug can't take down equity trading and vice versa.

Reuses the same risk/execution/ledger stack as the equity ensemble:
  CryptoAgent → RiskAgent (daily/weekly loss halt still applies) →
  AgentRiskBridge (sizing + confidence gate) → dedup check →
  order_executor (already supports crypto notional orders) → trade_ledger

Cron (added directly via SSH):
  */15 * * * *  cd /home/mddnnbr/tading-bot && /usr/bin/python3 crypto_scheduler.py >> logs/crypto_scheduler.log 2>&1
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

LOG_FILE = BASE_DIR / "logs" / "crypto_scheduler.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("CryptoScheduler")

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100000"))


def run_crypto_tick() -> list[dict]:
    from crypto_agent import CryptoAgent
    from risk_agent import RiskAgent
    from agent_risk_bridge import AgentRiskBridge
    import trade_ledger as _ledger

    now = datetime.now(timezone.utc)
    log.info(f"── Crypto tick start {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ──")

    risk_status = RiskAgent().assess()
    if risk_status["halt_trading"]:
        log.warning(f"TRADING HALTED (risk agent): {risk_status['warnings']}")
        return []

    signals = CryptoAgent().generate_signals()
    log.info(f"CryptoAgent: {len(signals)} raw signal(s)")
    if not signals:
        return []

    bridge = AgentRiskBridge(account_balance=ACCOUNT_BALANCE)
    approved = []
    for signal in signals:
        # Alpaca crypto is spot-only — no shorting. A "short" here would try
        # to sell coins we don't hold, which always fails at the exchange.
        # Skip gracefully rather than repeatedly hitting a doomed API call.
        if signal["direction"] == "short":
            log.info(f"⏭  SKIPPED: {signal['symbol']} short — Alpaca crypto is spot-only, shorting unsupported")
            continue

        side = "LONG"
        if _ledger.has_open_position(signal["symbol"], side):
            log.info(f"⏭  SKIPPED: {signal['symbol']} {signal['direction']} — already open")
            continue

        result = bridge.evaluate_signal(signal)
        if not result["approved"]:
            log.info(f"⛔ REJECTED: {signal['symbol']} — {result.get('rejection_reason')}")
            continue

        log.info(f"✅ APPROVED: {signal['symbol']} {signal['direction']} conf={signal['confidence']:.2f}")
        from order_executor import execute_signal
        execute_signal(result)
        approved.append(result)

    log.info(f"── Crypto tick done: {len(signals)} raw → {len(approved)} approved ──")
    return approved


def manage_crypto_exits() -> None:
    """Close crypto positions at Alpaca when price crosses stop or target.

    Alpaca crypto doesn't support bracket orders, so the entry market order
    carries no exchange-side exit — without this, coins bought by the bot sit
    in the account forever while the ledger merely simulates the close. Each
    tick, compare live price against the ledger's stop/target and submit a
    real market sell when either is crossed (spot-only account: exits are
    always sells of a long).
    """
    import trade_ledger as _ledger
    import yfinance as yf

    YF_MAP = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}

    open_crypto = [t for t in _ledger.open_positions() if t.symbol in YF_MAP]
    if not open_crypto:
        return

    for t in open_crypto:
        try:
            hist = yf.Ticker(YF_MAP[t.symbol]).history(period="1d", interval="5m")
            if hist is None or hist.empty:
                continue
            price = float(hist["Close"].iloc[-1])

            hit_target = price >= t.target_price
            hit_stop   = price <= t.stop_price
            if not (hit_target or hit_stop):
                continue

            reason = "target" if hit_target else "stop"
            log.info(f"🚪 EXIT {t.symbol}: price ${price:,.2f} crossed {reason} "
                     f"(target ${t.target_price:,.2f} / stop ${t.stop_price:,.2f}) — selling")

            from order_executor import get_executor
            executor = get_executor()
            if executor._client is None:
                log.warning("No Alpaca client — exit logged only")
                continue

            # close_position liquidates the whole holding for the symbol —
            # simpler and safer than hand-computing a sell quantity that
            # could drift from the actual filled amount.
            # Alpaca's position API uses the no-slash form (BTCUSD).
            executor._client.close_position(t.symbol.replace("/", ""))
        except Exception as e:
            log.warning(f"Crypto exit check failed for {t.symbol}: {e}")


if __name__ == "__main__":
    try:
        run_crypto_tick()
        manage_crypto_exits()
    except Exception as e:
        log.error(f"Crypto tick failed: {e}", exc_info=True)
        sys.exit(1)
