"""
macro_agent.py
--------------
Generates signals based on macroeconomic conditions and Fed policy signals.

Indicators monitored:
  - Treasury yield curve (2yr vs 10yr spread) — inversion = bearish
  - DXY (US Dollar strength) — strong dollar = bearish equities
  - Gold (GLD) vs equities — flight to safety signal
  - VIX level — fear gauge
  - TLT (20yr Treasury ETF) — bond market direction
  - Sector rotation: XLU/XLP outperforming = defensive, risk-off

Strategy:
  RISK-ON  → long equity ETFs (SPY, QQQ) when macro is supportive
  RISK-OFF → short equity ETFs or long defensive sectors when macro deteriorates
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("MacroAgent")

try:
    import yfinance as yf
    import pandas as pd
    _YF_OK = True
except ImportError:
    _YF_OK = False

# Macro instruments
SPY_SYMBOL  = "SPY"
QQQ_SYMBOL  = "QQQ"
TLT_SYMBOL  = "TLT"    # 20yr Treasury — rising = lower rates = good for growth
GLD_SYMBOL  = "GLD"    # Gold — rising = fear/inflation
DXY_SYMBOL  = "UUP"    # Dollar ETF proxy
XLU_SYMBOL  = "XLU"    # Utilities — defensive
XLP_SYMBOL  = "XLP"    # Consumer staples — defensive
VIX_SYMBOL  = "^VIX"

YIELD_2Y    = "^IRX"   # 13-week T-bill proxy for short rates
YIELD_10Y   = "^TNX"   # 10-year Treasury yield

VIX_FEAR    = 22.0     # above this = elevated fear
VIX_PANIC   = 30.0     # above this = panic / potential reversal
STOP_LOSS_PCT = 0.02
TARGET_PCT    = 0.05
MIN_CONFIDENCE = 0.55
EXPIRY_DAYS   = 21     # longer timeframe for macro plays


class MacroAgent:
    name = "MacroAgent"

    def __init__(self):
        self.regime_affinity = ["BULL_TREND", "BEAR_TREND", "HIGH_VOL", "LOW_VOL", "NEUTRAL"]

    def generate_signals(self) -> list[dict]:
        if not _YF_OK:
            return []
        try:
            return self._analyze()
        except Exception as e:
            log.warning(f"MacroAgent error: {e}")
            return []

    def _analyze(self) -> list[dict]:
        signals = []

        spy_df  = self._fetch(SPY_SYMBOL,  "3mo")
        qqq_df  = self._fetch(QQQ_SYMBOL,  "3mo")
        tlt_df  = self._fetch(TLT_SYMBOL,  "3mo")
        gld_df  = self._fetch(GLD_SYMBOL,  "3mo")
        dxy_df  = self._fetch(DXY_SYMBOL,  "3mo")
        xlu_df  = self._fetch(XLU_SYMBOL,  "3mo")
        xlp_df  = self._fetch(XLP_SYMBOL,  "3mo")
        vix_df  = self._fetch(VIX_SYMBOL,  "5d")

        if spy_df is None:
            return []

        price_spy = float(spy_df["Close"].iloc[-1])
        vix = float(vix_df["Close"].iloc[-1]) if vix_df is not None and not vix_df.empty else 20.0

        risk_on_factors  = []
        risk_off_factors = []

        # TLT direction (bonds): rising bonds = falling rates = risk-on
        if tlt_df is not None and len(tlt_df) >= 10:
            tlt_roc = float((tlt_df["Close"].iloc[-1] / tlt_df["Close"].iloc[-11] - 1) * 100)
            if tlt_roc > 1.5:
                risk_on_factors.append(f"Bonds rising (TLT +{tlt_roc:.1f}% 10d) → falling rates")
            elif tlt_roc < -1.5:
                risk_off_factors.append(f"Bonds falling (TLT {tlt_roc:.1f}% 10d) → rising rates")

        # Gold: rising gold = fear/inflation = risk-off
        if gld_df is not None and len(gld_df) >= 10:
            gld_roc = float((gld_df["Close"].iloc[-1] / gld_df["Close"].iloc[-11] - 1) * 100)
            if gld_roc > 2.0:
                risk_off_factors.append(f"Gold surging (GLD +{gld_roc:.1f}% 10d) → fear/inflation")
            elif gld_roc < -1.0:
                risk_on_factors.append(f"Gold declining (GLD {gld_roc:.1f}% 10d) → risk appetite")

        # Dollar: rising dollar = headwind for equities
        if dxy_df is not None and len(dxy_df) >= 10:
            dxy_roc = float((dxy_df["Close"].iloc[-1] / dxy_df["Close"].iloc[-11] - 1) * 100)
            if dxy_roc > 1.0:
                risk_off_factors.append(f"Strong dollar (UUP +{dxy_roc:.1f}% 10d) → equity headwind")
            elif dxy_roc < -0.5:
                risk_on_factors.append(f"Weak dollar (UUP {dxy_roc:.1f}% 10d) → equity tailwind")

        # Defensive rotation: XLU/XLP outperforming SPY = risk-off
        spy_roc_10 = float((spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-11] - 1) * 100)
        for etf, df_etf, label in [(XLU_SYMBOL, xlu_df, "Utilities"), (XLP_SYMBOL, xlp_df, "Staples")]:
            if df_etf is not None and len(df_etf) >= 11:
                etf_roc = float((df_etf["Close"].iloc[-1] / df_etf["Close"].iloc[-11] - 1) * 100)
                if etf_roc > spy_roc_10 + 2.0:
                    risk_off_factors.append(f"Defensive rotation into {label} ({etf_roc:.1f}% vs SPY {spy_roc_10:.1f}%)")

        # VIX: panic level can signal mean-reversion (contrarian long)
        if vix > VIX_PANIC:
            risk_on_factors.append(f"VIX panic level ({vix:.1f}) → contrarian buy signal")
        elif vix > VIX_FEAR:
            risk_off_factors.append(f"Elevated VIX ({vix:.1f}) → caution")
        elif vix < 15:
            risk_on_factors.append(f"Low VIX ({vix:.1f}) → complacency / trend-friendly")

        # Build signals
        if len(risk_on_factors) >= 2:
            sig = self._build_signal(SPY_SYMBOL, price_spy, "long", risk_on_factors)
            if sig:
                signals.append(sig)

        if len(risk_off_factors) >= 2:
            sig_qqq = self._build_signal(QQQ_SYMBOL,
                                          float(qqq_df["Close"].iloc[-1]) if qqq_df is not None else price_spy,
                                          "short", risk_off_factors)
            if sig_qqq:
                signals.append(sig_qqq)

        return signals

    def _build_signal(self, symbol: str, price: float, direction: str, factors: list) -> Optional[dict]:
        n = len(factors)
        conf_map = {2: 0.58, 3: 0.67, 4: 0.74, 5: 0.80}
        confidence = conf_map.get(n, 0.83 if n >= 6 else 0.55)

        if confidence < MIN_CONFIDENCE:
            return None

        if direction == "long":
            stop   = round(price * (1 - STOP_LOSS_PCT), 2)
            target = round(price * (1 + TARGET_PCT), 2)
            strat  = "single_leg_calls"
        else:
            stop   = round(price * (1 + STOP_LOSS_PCT), 2)
            target = round(price * (1 - TARGET_PCT), 2)
            strat  = "single_leg_puts"

        return {
            "agent":           self.name,
            "strategy":        strat,
            "instrument_type": "options",
            "symbol":          symbol,
            "direction":       direction,
            "entry_price":     round(price, 2),
            "stop_loss_price": stop,
            "target_price":    target,
            "option_premium":  None,
            "futures_symbol":  None,
            "confidence":      confidence,
            "expiration":      _next_friday(EXPIRY_DAYS),
            "meta_score":      confidence,
            "regime_affinity": self.regime_affinity,
            "reasons":         factors,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _fetch(symbol: str, period: str):
        try:
            df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
            return df if not df.empty else None
        except Exception:
            return None


def _next_friday(days_out: int) -> str:
    today  = datetime.now(timezone.utc).date()
    target = today + timedelta(days=days_out)
    days_to_friday = (4 - target.weekday()) % 7
    return (target + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    agent = MacroAgent()
    sigs  = agent.generate_signals()
    print(f"MacroAgent: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s['symbol']} {s['direction']} conf={s['confidence']} | {s['reasons']}")
