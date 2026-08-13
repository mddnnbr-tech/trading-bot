"""
replay.py
─────────
Historical replay of the trade MECHANICS: entry geometry, position sizing,
initial stop, and the progressive trailing stop — exactly as production
computes them.

Why this exists. 174 live trades produced an average of -$1 with a 95% CI of
-$79..+$77: an edge indistinguishable from zero, and far too slow a clock to
judge a change by. At ~7 entries per 3 days, validating one parameter would
take six weeks. This generates the same number of trades in under a minute,
so a parameter can be argued with evidence instead of intuition.

What is faithful, and what is a proxy — the distinction matters when reading
the output:

  FAITHFUL (lifted from production, same formulas)
    • ATR(14) stop geometry and its cap        ensemble._normalize_geometry
    • risk-budget sizing, notional cap         agent_risk_bridge
    • progressive trail by profit tier         order_executor._trail_for_profit
    • max hold horizon                         trade_ledger.MAX_HOLD_DAYS

  PROXY (production uses live screens/news that cannot be replayed)
    • the SIGNAL layer. Momentum/breakout/mean-reversion rules stand in for
      the 15 agents. So this measures whether the MECHANICS convert a given
      signal into money — not whether the agents pick well. A mechanics
      change that wins here should win live on the same signals; it says
      nothing about signal quality itself.

Lookahead is avoided deliberately: indicators for day t use bars strictly
before t, and entry fills at the OPEN of t+1. Exits are checked against the
daily high/low, filling at the stop price. Intraday path is unknowable from
daily bars, so when both the stop and a new high occur in one session this
assumes the stop filled first — the pessimistic ordering.
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Production constants, mirrored ──────────────────────────────────────────
ATR_MULT           = 1.5
RISK_PER_TRADE_PCT = 0.5
MAX_POSITION_PCT   = 10.0
MAX_HOLD_DAYS      = 30
ACCOUNT            = 86_500.0


def trail_for_profit(pct_gain: float) -> float:
    """order_executor._trail_for_profit — tighten as a winner extends."""
    if pct_gain >= 15:
        return 3.0
    if pct_gain >= 8:
        return 4.0
    if pct_gain >= 4:
        return 5.5
    return 8.0


UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "AVGO",
    "NFLX", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU", "SMCI", "PLTR",
    "COIN", "HOOD", "SHOP", "UBER", "ABNB", "DAL", "BA", "CAT", "DE",
    "XOM", "CVX", "COP", "SLB", "OXY", "JPM", "GS", "BAC", "WFC", "C",
    "UNH", "JNJ", "PFE", "MRK", "LLY", "WMT", "COST", "TGT", "HD", "NKE",
    "DIS", "T", "VZ", "SPY", "QQQ", "IWM", "XLE", "XLF", "GME", "RIVN",
]


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_date: object
    entry: float
    shares: float
    stop: float
    exit_date: object = None
    exit: float = 0.0
    pnl: float = 0.0
    reason: str = ""
    held: int = 0
    mfe_pct: float = 0.0     # max favourable excursion — how far it ran for us
    recovered: bool = False  # after a stop, did it come back past entry


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def signals_for(df: pd.DataFrame, i: int) -> str | None:
    """Signal from bars strictly before i. Proxy for the agent ensemble."""
    if i < 60:
        return None
    c = df["Close"]
    sma20 = c.iloc[i - 20:i].mean()
    sma50 = c.iloc[i - 50:i].mean()
    px = c.iloc[i - 1]
    r = df["_rsi"].iloc[i - 1]
    hi20 = df["High"].iloc[i - 20:i].max()
    lo20 = df["Low"].iloc[i - 20:i].min()
    chg = (c.iloc[i - 1] / c.iloc[i - 2] - 1) * 100 if i >= 2 else 0
    if np.isnan(r) or np.isnan(sma20):
        return None
    # Breakout / momentum long
    if px >= hi20 and px > sma20 > sma50 and 50 < r < 75:
        return "long"
    # Big one-day gainer (MoversAgent proxy)
    if chg >= 5 and px > sma20:
        return "long"
    # Breakdown / momentum short
    if px <= lo20 and px < sma20 < sma50 and 25 < r < 50:
        return "short"
    if chg <= -5 and px < sma20:
        return "short"
    return None


def run(stop_cap_pct: float | None, use_progressive_trail: bool,
       fixed_trail: float | None, bars: dict) -> list[Trade]:
    trades: list[Trade] = []
    for sym, df in bars.items():
        i = 60
        open_until = -1
        while i < len(df) - 1:
            if i <= open_until:
                i += 1
                continue
            d = signals_for(df, i)
            if d is None:
                i += 1
                continue
            a = df["_atr"].iloc[i - 1]
            entry = float(df["Open"].iloc[i])
            if not np.isfinite(a) or a <= 0 or entry <= 0:
                i += 1
                continue

            stop_dist = max(ATR_MULT * a, entry * 0.01)
            if stop_cap_pct is not None:
                stop_dist = min(stop_dist, entry * stop_cap_pct)

            risk_budget = ACCOUNT * RISK_PER_TRADE_PCT / 100
            shares = min(risk_budget / stop_dist,
                         ACCOUNT * MAX_POSITION_PCT / 100 / entry)
            if shares < 1:
                i += 1
                continue

            long = d == "long"
            stop = entry - stop_dist if long else entry + stop_dist
            peak = entry
            t = Trade(sym, d, df.index[i], entry, shares, stop)

            for j in range(i + 1, min(i + 1 + MAX_HOLD_DAYS, len(df))):
                hi, lo = float(df["High"].iloc[j]), float(df["Low"].iloc[j])
                # favourable excursion so far
                fav = (hi / entry - 1) * 100 if long else (entry / lo - 1) * 100
                t.mfe_pct = max(t.mfe_pct, fav)
                # Pessimistic ordering: test the stop before extending it.
                if (long and lo <= stop) or (not long and hi >= stop):
                    t.exit_date, t.exit = df.index[j], stop
                    t.reason = "stop"
                    break
                peak = max(peak, hi) if long else min(peak, lo)
                gain = (peak / entry - 1) * 100 if long else (entry / peak - 1) * 100
                tr = (trail_for_profit(gain) if use_progressive_trail
                      else (fixed_trail if fixed_trail else 8.0))
                new_stop = peak * (1 - tr / 100) if long else peak * (1 + tr / 100)
                stop = max(stop, new_stop) if long else min(stop, new_stop)
            else:
                j = min(i + MAX_HOLD_DAYS, len(df) - 1)
                t.exit_date, t.exit = df.index[j], float(df["Close"].iloc[j])
                t.reason = "timeout"

            t.pnl = ((t.exit - entry) if long else (entry - t.exit)) * shares
            t.held = max(1, (t.exit_date - t.entry_date).days)
            # Did it recover past entry within 10 sessions after we exited?
            k = df.index.get_loc(t.exit_date)
            after = df.iloc[k + 1:k + 11]
            if len(after) and t.reason == "stop":
                t.recovered = (float(after["High"].max()) > entry if long
                               else float(after["Low"].min()) < entry)
            trades.append(t)
            open_until = k
            i += 1
    return trades


def summarize(name: str, ts: list[Trade]) -> dict:
    if not ts:
        return {"config": name, "n": 0}
    p = np.array([t.pnl for t in ts])
    w = p[p > 0]
    l = p[p <= 0]
    sd = p.std(ddof=1) if len(p) > 1 else 0.0
    se = sd / np.sqrt(len(p)) if len(p) else 0.0
    stopped = [t for t in ts if t.reason == "stop"]
    rec = [t for t in stopped if t.recovered]
    return {
        "config": name, "n": len(p), "total": p.sum(), "avg": p.mean(),
        "ci_lo": p.mean() - 1.96 * se, "ci_hi": p.mean() + 1.96 * se,
        "win%": len(w) / len(p) * 100,
        "avg_w": w.mean() if len(w) else 0, "avg_l": l.mean() if len(l) else 0,
        "pf": (w.sum() / abs(l.sum())) if len(l) and l.sum() else float("inf"),
        "shaken%": (len(rec) / len(stopped) * 100) if stopped else 0,
        "hold": np.mean([t.held for t in ts]),
    }


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "2y"
    print(f"downloading {len(UNIVERSE)} symbols, {period} of daily bars...")
    raw = yf.download(UNIVERSE, period=period, interval="1d",
                      group_by="ticker", progress=False, auto_adjust=True)
    bars = {}
    for s in UNIVERSE:
        try:
            df = raw[s].dropna().copy()
            if len(df) < 120:
                continue
            df["_atr"] = atr(df)
            df["_rsi"] = rsi(df["Close"])
            bars[s] = df
        except Exception:
            continue
    print(f"usable symbols: {len(bars)}   sessions: "
          f"{min(len(d) for d in bars.values())}-{max(len(d) for d in bars.values())}\n")

    configs = [
        ("stop 4%  + progressive trail  [OLD PRODUCTION]", 0.04, True, None),
        ("stop 8%  + progressive trail", 0.08, True, None),
        ("stop 12% + progressive trail  [NEW PRODUCTION]", 0.12, True, None),
        ("stop uncapped + progressive trail", None, True, None),
        ("stop 12% + fixed 8% trail", 0.12, False, 8.0),
        ("stop 12% + fixed 5.5% trail", 0.12, False, 5.5),
        ("stop 4%  + fixed 8% trail", 0.04, False, 8.0),
    ]
    rows = []
    for name, cap, prog, ft in configs:
        rows.append(summarize(name, run(cap, prog, ft, bars)))

    hdr = ("{:<46}{:>6}{:>11}{:>9}{:>18}{:>7}{:>9}{:>9}{:>8}{:>7}")
    print(hdr.format("config", "n", "total$", "avg$", "95% CI", "win%",
                     "avgWin", "avgLoss", "PF", "shake%"))
    print("-" * 130)
    for r in sorted(rows, key=lambda x: -x.get("total", 0)):
        if not r["n"]:
            continue
        print(("{:<46}{:>6d}{:>11,.0f}{:>9,.0f}"
               "{:>18}{:>7.0f}{:>9,.0f}{:>9,.0f}{:>8.2f}{:>7.0f}").format(
            r["config"], r["n"], r["total"], r["avg"],
            "{:+,.0f}..{:+,.0f}".format(r["ci_lo"], r["ci_hi"]),
            r["win%"], r["avg_w"], r["avg_l"], r["pf"], r["shaken%"]))
    print("\nshake% = of trades stopped out, the share that then recovered "
          "past our entry within 10 sessions (lower is better)")
    print("PF = profit factor (gross wins / gross losses); >1.0 is profitable")
    print("CI excluding 0 means the edge is real at 95% confidence")


if __name__ == "__main__":
    main()
