"""
deep_research.py
────────────────
Second-pass signal research: ~35 years, regime-conditioned, with ablations.

signal_research.py covered 2005-2026 and ranked buy-the-dip above breakout.
That window contains 2008 and 2020, but it is still one macro era — ZIRP,
QE, and a secular bull in mega-cap tech. This extends to 1990 so the sample
includes the 1990-91 recession, the dot-com melt-up AND its 78% Nasdaq
collapse, and the 1994 and 2022 rate shocks.

Four questions this answers that the first pass could not:

  1. DECADE STABILITY — does the edge hold in every decade, or was it an
     artifact of the post-2009 dip-buying regime? A rule that works only
     when the Fed backstops every drawdown is not a rule.
  2. REGIME CONDITIONING — bull vs bear vs high-volatility. An agent that
     knows WHEN its signal works can stand down instead of bleeding.
  3. TREND FILTER ABLATION — price>SMA200 is asserted to be what separates
     a pullback from a falling knife. Measure it rather than assume it.
  4. PARAMETER SENSITIVITY — if the edge collapses when RSI moves 30->35,
     it is curve-fit. A real edge degrades gracefully.

Survivorship bias is unchanged and still bounds every absolute number: the
universe is companies still listed today, which over 35 years is a far
stronger filter than over 20. Treat cross-signal RANKINGS and regime
CONTRASTS as the findings; treat absolute expectancy as an upper bound.
"""

from __future__ import annotations

import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ATR_MULT, STOP_CAP = 1.5, 0.04
RISK_PCT, MAX_POS_PCT = 0.5, 10.0
MAX_HOLD, ACCOUNT = 30, 86_500.0
START = "1990-01-01"

# Chosen for LENGTH of listed history, not recent performance.
UNIVERSE = [
    "AAPL","MSFT","IBM","INTC","ORCL","CSCO","QCOM","TXN","AMD","MU","HPQ",
    "KO","PG","JNJ","MRK","PFE","ABT","BMY","AMGN","LLY","UNH",
    "XOM","CVX","COP","SLB","HAL",
    "JPM","BAC","WFC","C","AXP","GS",
    "WMT","HD","LOW","TGT","COST","MCD","SBUX","NKE","PEP",
    "DIS","T","VZ","BA","CAT","DE","MMM","GE","F","UPS","FDX","SPY",
]


def prep(df):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    d = c.diff()
    df["rsi"] = 100 - 100 / (1 + d.clip(lower=0).rolling(14).mean()
                             / (-d.clip(upper=0)).rolling(14).mean().replace(0, np.nan))
    df["sma20"] = c.rolling(20).mean()
    df["sma50"] = c.rolling(50).mean()
    df["sma200"] = c.rolling(200).mean()
    df["hh20"] = h.rolling(20).max()
    sd = c.rolling(20).std()
    df["bb_lo"] = df["sma20"] - 2 * sd
    df["vol20"] = v.rolling(20).mean()
    return df


def trail(g):
    return 3.0 if g >= 15 else 4.0 if g >= 8 else 5.5 if g >= 4 else 8.0


def sim(df, rule, short=False, regime=None):
    """Returns (year, pnl, regime_label) per trade."""
    out, n = [], len(df)
    rows = list(df.itertuples())
    i, busy = 210, -1
    while i < n - 1:
        if i <= busy:
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
        sd_ = min(max(ATR_MULT * p.atr, entry * 0.01), entry * STOP_CAP)
        sh = min(ACCOUNT * RISK_PCT / 100 / sd_, ACCOUNT * MAX_POS_PCT / 100 / entry)
        if sh < 1:
            i += 1
            continue
        stop = entry + sd_ if short else entry - sd_
        peak, ex, k = entry, None, i
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            hi, lo = float(df["High"].iloc[j]), float(df["Low"].iloc[j])
            if (not short and lo <= stop) or (short and hi >= stop):
                ex, k = stop, j
                break
            peak = min(peak, lo) if short else max(peak, hi)
            g = (entry / peak - 1) * 100 if short else (peak / entry - 1) * 100
            t = trail(g)
            ns = peak * (1 + t / 100) if short else peak * (1 - t / 100)
            stop = min(stop, ns) if short else max(stop, ns)
        if ex is None:
            k = min(i + MAX_HOLD, n - 1)
            ex = float(df["Close"].iloc[k])
        pnl = ((entry - ex) if short else (ex - entry)) * sh
        ts = df.index[i]
        out.append((ts.year, pnl, regime.get(ts, "?") if regime else "?"))
        busy = k
        i += 1
    return out


def stats(pnls):
    a = np.array(pnls)
    if len(a) < 30:
        return None
    w, l = a[a > 0], a[a <= 0]
    se = a.std(ddof=1) / np.sqrt(len(a))
    return {"n": len(a), "avg": a.mean(), "lo": a.mean() - 1.96 * se,
            "pf": (w.sum() / abs(l.sum())) if len(l) and l.sum() else np.inf}


def main():
    print(f"downloading {len(UNIVERSE)} symbols from {START} ...", flush=True)
    raw = yf.download(UNIVERSE, start=START, interval="1d", group_by="ticker",
                      progress=False, auto_adjust=True, threads=True)
    bars = {}
    for s in UNIVERSE:
        try:
            d = raw[s].dropna().copy()
            if len(d) > 2500:
                bars[s] = prep(d)
        except Exception:
            continue
    spans = [(d.index[0].year, d.index[-1].year) for d in bars.values()]
    print(f"usable: {len(bars)} symbols, earliest {min(a for a, _ in spans)}, "
          f"{sum(len(d) for d in bars.values()):,} symbol-days\n", flush=True)

    # Regime from SPY: bull/bear by 200-day trend, plus a high-volatility cut.
    spy = bars.get("SPY")
    regime = {}
    if spy is not None:
        r20 = spy["Close"].pct_change().rolling(20).std() * np.sqrt(252) * 100
        for ts, row in spy.iterrows():
            if not np.isfinite(row["sma200"]):
                continue
            v = r20.get(ts, np.nan)
            regime[ts] = ("HIGH-VOL" if np.isfinite(v) and v > 25 else
                          "BULL" if row["Close"] > row["sma200"] else "BEAR")

    RULES = {
        "bb_reversion_long":   (lambda p, pp: p.Close < p.bb_lo and p.Close > p.sma200, False),
        "rsi_oversold_long":   (lambda p, pp: p.rsi < 30 and p.Close > p.sma200, False),
        "trend_pullback_long": (lambda p, pp: p.Close > p.sma200 and p.Close < p.sma20 and 35 < p.rsi < 50, False),
        "breakout20_long":     (lambda p, pp: p.Close >= p.hh20 and p.Close > p.sma50, False),
    }

    print("=" * 100)
    print("1. DECADE STABILITY  (avg $/trade; a real edge shows up in every decade)")
    print("=" * 100)
    buckets = [(1990, 1999), (2000, 2009), (2010, 2019), (2020, 2026)]
    print("{:<22}{:>10}".format("signal", "n") + "".join(
        "{:>16}".format(f"{a}-{b}") for a, b in buckets))
    res = {}
    for nm, (rule, sh) in RULES.items():
        tr = []
        for d in bars.values():
            tr += sim(d, rule, sh, regime)
        res[nm] = tr
        row = "{:<22}{:>10d}".format(nm, len(tr))
        for a, b in buckets:
            s = stats([p for y, p, _ in tr if a <= y <= b])
            row += "{:>16}".format(f"{s['avg']:+,.0f} ({s['n']})" if s else "--")
        print(row, flush=True)

    print("\n" + "=" * 100)
    print("2. REGIME CONDITIONING  (avg $/trade | PF)")
    print("=" * 100)
    print("{:<22}{:>22}{:>22}{:>22}".format("signal", "BULL", "BEAR", "HIGH-VOL"))
    for nm, tr in res.items():
        row = "{:<22}".format(nm)
        for rg in ("BULL", "BEAR", "HIGH-VOL"):
            s = stats([p for _, p, r in tr if r == rg])
            row += "{:>22}".format(f"{s['avg']:+,.0f} | PF {s['pf']:.2f} ({s['n']})" if s else "--")
        print(row, flush=True)

    print("\n" + "=" * 100)
    print("3. TREND-FILTER ABLATION  (is price>SMA200 doing real work?)")
    print("=" * 100)
    for nm, base in (("bb_reversion", lambda p, pp: p.Close < p.bb_lo),
                     ("rsi_oversold", lambda p, pp: p.rsi < 30)):
        for lbl, rule in ((f"{nm} WITH sma200 filter",
                           lambda p, pp, b=base: b(p, pp) and p.Close > p.sma200),
                          (f"{nm} NO filter",
                           lambda p, pp, b=base: b(p, pp)),
                          (f"{nm} BELOW sma200 only",
                           lambda p, pp, b=base: b(p, pp) and p.Close < p.sma200)):
            tr = []
            for d in bars.values():
                tr += sim(d, rule, False)
            s = stats([p for _, p, _ in tr])
            print("  {:<38}{}".format(lbl, f"n={s['n']:<6} avg {s['avg']:+,.0f}  "
                  f"PF {s['pf']:.2f}  CIlo {s['lo']:+,.0f}" if s else "insufficient"), flush=True)

    print("\n" + "=" * 100)
    print("4. PARAMETER SENSITIVITY  (graceful degradation = real; cliff = curve-fit)")
    print("=" * 100)
    for th in (20, 25, 30, 35, 40):
        tr = []
        for d in bars.values():
            tr += sim(d, lambda p, pp, t=th: p.rsi < t and p.Close > p.sma200, False)
        s = stats([p for _, p, _ in tr])
        print("  RSI < {:<4}{}".format(th, f"n={s['n']:<6} avg {s['avg']:+,.0f}  "
              f"PF {s['pf']:.2f}" if s else "insufficient"), flush=True)
    for mult in (1.0, 1.5, 2.0, 2.5):
        tr = []
        for d in bars.values():
            tr += sim(d, lambda p, pp, m=mult: p.Close < p.sma20 - m * (p.sma20 - p.bb_lo) / 2
                      and p.Close > p.sma200, False)
        s = stats([p for _, p, _ in tr])
        print("  dip {:.1f} SD  {}".format(mult, f"n={s['n']:<6} avg {s['avg']:+,.0f}  "
              f"PF {s['pf']:.2f}" if s else "insufficient"), flush=True)


if __name__ == "__main__":
    main()
