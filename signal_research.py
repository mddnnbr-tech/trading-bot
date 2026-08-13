"""
signal_research.py
──────────────────
Which entry signals actually carry an edge, measured over ~20 years and
scored on CONSISTENCY rather than total profit.

Motivation. replay.py established that the exit mechanics are sound: a naive
20-day breakout run through production's stop/trail/sizing earns ~+$89 a
trade at PF 1.52, while the live 15-agent ensemble earns -$1 at PF ~1.0 on
identical machinery. Same win rate, opposite expectancy. So the edge is
being lost in the SIGNAL layer, and the useful question is which signals
survive out-of-sample.

Method
  • Each candidate is an independent boolean rule evaluated on bars strictly
    before the signal day; entry fills at the next open.
  • Every candidate is then run through the SAME production mechanics —
    ATR(14)x1.5 stop capped at 4%, progressive trail, risk-budget sizing —
    so differences reflect the signal, never the plumbing.
  • Scored per calendar year. A rule that earns its whole edge in two lucky
    years is worthless for a bot that must trade every year, so the headline
    metric is the share of years profitable, not the total.

Known limitation, stated because it bounds every conclusion: the universe is
symbols listed TODAY, so it inherits survivorship bias and the long side is
flattered. Comparisons BETWEEN signals share that bias and stay meaningful;
absolute expectancy does not. Point-in-time constituents would be needed to
fix it, which yfinance cannot provide.
"""

from __future__ import annotations

import sys
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ATR_MULT           = 1.5
STOP_CAP           = 0.04
RISK_PER_TRADE_PCT = 0.5
MAX_POSITION_PCT   = 10.0
MAX_HOLD_DAYS      = 30
ACCOUNT            = 86_500.0
START              = "2005-01-01"

# Deliberately mixed: mega-cap winners, long-term laggards (INTC, WBA, T,
# VZ, F, IBM, CSCO), cyclicals and sector ETFs. Cannot remove survivorship
# bias, but avoids a universe made only of the last decade's winners.
UNIVERSE = [
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AMD","AVGO","NFLX",
    "CRM","ORCL","ADBE","INTC","QCOM","MU","IBM","CSCO","TXN","HPQ",
    "JPM","GS","BAC","WFC","C","AXP","MS","USB","SCHW",
    "XOM","CVX","COP","SLB","OXY","HAL","PSX",
    "UNH","JNJ","PFE","MRK","LLY","ABT","BMY","AMGN","GILD","WBA",
    "WMT","COST","TGT","HD","LOW","NKE","SBUX","MCD","KO","PG","PEP",
    "DIS","T","VZ","CMCSA","F","GM","GE","CAT","DE","BA","MMM","UPS","FDX",
    "SPY","QQQ","IWM","DIA","XLE","XLF","XLK","XLV","XLI","XLP","XLU","GLD",
]


def prep(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    d = c.diff()
    up, dn = d.clip(lower=0).rolling(14).mean(), (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    for n in (10, 20, 50, 200):
        df[f"sma{n}"] = c.rolling(n).mean()
    df["hh20"], df["ll20"] = h.rolling(20).max(), l.rolling(20).min()
    df["hh52"] = h.rolling(252).max()
    df["vol20"] = v.rolling(20).mean()
    df["chg1"] = c.pct_change() * 100
    df["ret12_1"] = (c.shift(21) / c.shift(252) - 1) * 100     # 12-1 momentum
    df["atrpct"] = df["atr"] / c * 100
    df["atrpct_ma"] = df["atrpct"].rolling(50).mean()
    ema12, ema26 = c.ewm(span=12).mean(), c.ewm(span=26).mean()
    df["macd"] = ema12 - ema26
    df["macdsig"] = df["macd"].ewm(span=9).mean()
    sd = c.rolling(20).std()
    df["bb_lo"] = df["sma20"] - 2 * sd
    df["bb_up"] = df["sma20"] + 2 * sd
    return df


# Each rule reads row i-1 (previous close) and returns True to enter at open i.
# p = prior row, pp = row before that.
SIGNALS = {
    "breakout20_long":      lambda p, pp: p.Close >= p.hh20 and p.Close > p.sma50,
    "breakout52w_long":     lambda p, pp: p.Close >= p.hh52 * 0.99 and p.Close > p.sma50,
    "trend_pullback_long":  lambda p, pp: p.Close > p.sma200 and p.Close < p.sma20 and 35 < p.rsi < 50,
    "golden_trend_long":    lambda p, pp: p.sma50 > p.sma200 and p.Close > p.sma20 and 50 < p.rsi < 70,
    "mom12_1_long":         lambda p, pp: p.ret12_1 > 20 and p.Close > p.sma50,
    "macd_cross_long":      lambda p, pp: p.macd > p.macdsig and pp.macd <= pp.macdsig and p.Close > p.sma50,
    "rsi_oversold_long":    lambda p, pp: p.rsi < 30 and p.Close > p.sma200,
    "bb_reversion_long":    lambda p, pp: p.Close < p.bb_lo and p.Close > p.sma200,
    "gap_up5_long":         lambda p, pp: p.chg1 >= 5 and p.Close > p.sma20,
    "vol_surge_long":       lambda p, pp: p.Volume > 2 * p.vol20 and p.chg1 > 2 and p.Close > p.sma50,
    "vol_squeeze_long":     lambda p, pp: p.atrpct < 0.7 * p.atrpct_ma and p.Close >= p.hh20,
    "breakdown20_short":    lambda p, pp: p.Close <= p.ll20 and p.Close < p.sma50,
    "death_trend_short":    lambda p, pp: p.sma50 < p.sma200 and p.Close < p.sma20 and 30 < p.rsi < 50,
    "drop5_short":          lambda p, pp: p.chg1 <= -5 and p.Close < p.sma20,
    "rsi_overbought_short": lambda p, pp: p.rsi > 75 and p.Close < p.sma200,
    "mom_reversal_short":   lambda p, pp: p.ret12_1 < -20 and p.Close < p.sma50,
}


def trail_for_profit(g: float) -> float:
    return 3.0 if g >= 15 else 4.0 if g >= 8 else 5.5 if g >= 4 else 8.0


def simulate(df: pd.DataFrame, rule, is_short: bool):
    out = []
    n = len(df)
    cols = df.itertuples()
    rows = list(cols)
    i, open_until = 210, -1
    while i < n - 1:
        if i <= open_until:
            i += 1
            continue
        p, pp = rows[i - 1], rows[i - 2]
        try:
            fire = bool(rule(p, pp))
        except Exception:
            fire = False
        if not fire or not np.isfinite(p.atr) or p.atr <= 0:
            i += 1
            continue
        entry = float(df["Open"].iloc[i])
        if entry <= 0:
            i += 1
            continue
        stop_dist = min(max(ATR_MULT * p.atr, entry * 0.01), entry * STOP_CAP)
        shares = min(ACCOUNT * RISK_PER_TRADE_PCT / 100 / stop_dist,
                     ACCOUNT * MAX_POSITION_PCT / 100 / entry)
        if shares < 1:
            i += 1
            continue
        stop = entry + stop_dist if is_short else entry - stop_dist
        peak = entry
        exit_px, k = None, i
        for j in range(i + 1, min(i + 1 + MAX_HOLD_DAYS, n)):
            hi, lo = float(df["High"].iloc[j]), float(df["Low"].iloc[j])
            if (not is_short and lo <= stop) or (is_short and hi >= stop):
                exit_px, k = stop, j
                break
            peak = min(peak, lo) if is_short else max(peak, hi)
            g = (entry / peak - 1) * 100 if is_short else (peak / entry - 1) * 100
            t = trail_for_profit(g)
            ns = peak * (1 + t / 100) if is_short else peak * (1 - t / 100)
            stop = min(stop, ns) if is_short else max(stop, ns)
        if exit_px is None:
            k = min(i + MAX_HOLD_DAYS, n - 1)
            exit_px = float(df["Close"].iloc[k])
        pnl = ((entry - exit_px) if is_short else (exit_px - entry)) * shares
        out.append((df.index[i].year, pnl))
        open_until = k
        i += 1
    return out


def main():
    print(f"downloading {len(UNIVERSE)} symbols from {START} ...")
    raw = yf.download(UNIVERSE, start=START, interval="1d", group_by="ticker",
                      progress=False, auto_adjust=True, threads=True)
    bars = {}
    for s in UNIVERSE:
        try:
            d = raw[s].dropna().copy()
            if len(d) < 1500:
                continue
            bars[s] = prep(d)
        except Exception:
            continue
    yrs = sorted({y for d in bars.values() for y in d.index.year.unique()})
    print(f"usable: {len(bars)} symbols, {yrs[0]}-{yrs[-1]}, "
          f"{sum(len(d) for d in bars.values()):,} symbol-days\n")

    results = []
    for name, rule in SIGNALS.items():
        is_short = name.endswith("short")
        per_year = defaultdict(list)
        for d in bars.values():
            for y, pnl in simulate(d, rule, is_short):
                per_year[y].append(pnl)
        allp = np.array([p for v in per_year.values() for p in v])
        if len(allp) < 200:
            continue
        yearly = {y: float(np.sum(v)) for y, v in per_year.items() if len(v) >= 5}
        pos_years = sum(1 for v in yearly.values() if v > 0)
        sd = allp.std(ddof=1)
        se = sd / np.sqrt(len(allp))
        w, l = allp[allp > 0], allp[allp <= 0]
        results.append({
            "name": name, "n": len(allp), "avg": allp.mean(),
            "ci_lo": allp.mean() - 1.96 * se, "ci_hi": allp.mean() + 1.96 * se,
            "win": len(w) / len(allp) * 100,
            "pf": (w.sum() / abs(l.sum())) if len(l) and l.sum() else np.inf,
            "years": len(yearly), "pos_years": pos_years,
            "consist": pos_years / len(yearly) * 100 if yearly else 0,
            "worst_year": min(yearly.values()) if yearly else 0,
        })

    results.sort(key=lambda r: (-r["consist"], -r["avg"]))
    print("{:<22}{:>7}{:>8}{:>16}{:>6}{:>7}{:>10}{:>11}".format(
        "signal", "n", "avg$", "95% CI", "win%", "PF", "yrs pos", "worst yr$"))
    print("-" * 96)
    for r in results:
        sig = "" if r["ci_lo"] > 0 else "  (CI spans 0)"
        print("{:<22}{:>7d}{:>8,.0f}{:>16}{:>6.0f}{:>7.2f}{:>10}{:>11,.0f}{}".format(
            r["name"], r["n"], r["avg"],
            "{:+,.0f}..{:+,.0f}".format(r["ci_lo"], r["ci_hi"]),
            r["win"], r["pf"], f"{r['pos_years']}/{r['years']}",
            r["worst_year"], sig))

    keep = [r for r in results if r["ci_lo"] > 0 and r["consist"] >= 70 and r["pf"] > 1.15]
    print("\n" + "=" * 96)
    print("SURVIVORS — CI excludes zero, profitable in >=70% of years, PF > 1.15:")
    for r in keep:
        print("   {:<22} +${:.0f}/trade  PF {:.2f}  {}/{} years".format(
            r["name"], r["avg"], r["pf"], r["pos_years"], r["years"]))
    if not keep:
        print("   none — no signal cleared the bar")


if __name__ == "__main__":
    main()
