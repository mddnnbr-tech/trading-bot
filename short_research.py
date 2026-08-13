"""
short_research.py
─────────────────
Does the short side work, and if so where?

Prompted by a direct question: if a dip BELOW the 200-day is too dangerous
to buy, shouldn't we short it instead? That is the correct inverse to ask,
and it is testable on the same 1990-2026 panel.

Tests the mirror image of every long rule that worked, plus the specific
"weak stock below its 200-day" case:

    short_below_200        the literal inverse of buying a sub-200 dip
    short_rally_downtrend  mean reversion inverted — sell strength in a
                           downtrend, the mirror of buying weakness in an
                           uptrend, which was the best long rule found
    short_breakdown        new 20-day low with the trend already down
    short_rsi_overbought   overbought while below the 200-day
    short_death_cross      50 below 200, price below the 20-day

Also reports BEAR and HIGH_VOL separately, because a short book that only
has to work in drawdowns is still worth having — that is precisely when the
long book cannot help.

THE BIAS RUNS THE OTHER WAY HERE, and it is severe. The universe is
companies still listed in 2026, so every name in it survived. The stocks
that would have made shorts pay — the bankruptcies, the delistings, the
90% drawdowns that never recovered — are systematically ABSENT. Enron,
Lehman, Bear Stearns, Nortel, Sears, and every 2000 dot-com casualty are
missing by construction.

So these numbers are a FLOOR, not an estimate. A short rule that merely
breaks even here may be genuinely profitable in a universe that includes
the failures. Read a positive result as strong evidence, and a negative
result as inconclusive rather than damning.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ATR_MULT, STOP_CAP = 1.5, 0.04
RISK_PCT, MAX_POS_PCT = 0.5, 10.0
MAX_HOLD, ACCOUNT = 30, 86_500.0
START = "1990-01-01"

from deep_research import UNIVERSE, prep, trail, sim, stats  # noqa: E402


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
    print(f"usable: {len(bars)} symbols, "
          f"{sum(len(d) for d in bars.values()):,} symbol-days\n", flush=True)

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

    SHORTS = {
        "short_below_200":       lambda p, pp: p.Close < p.sma200 and p.Close < p.sma20,
        "short_rally_downtrend": lambda p, pp: p.Close < p.sma200 and p.rsi > 60,
        "short_rsi_overbought":  lambda p, pp: p.Close < p.sma200 and p.rsi > 70,
        "short_breakdown":       lambda p, pp: (p.Close < p.sma200 and p.sma50 < p.sma200
                                                and p.Close < p.sma20 and p.rsi < 40),
        "short_death_cross":     lambda p, pp: p.sma50 < p.sma200 and p.Close < p.sma20,
    }

    buckets = [(1990, 1999), (2000, 2009), (2010, 2019), (2020, 2026)]
    print("=" * 104)
    print("SHORT RULES — overall, then by decade   (bias makes these a FLOOR)")
    print("=" * 104)
    print("{:<24}{:>7}{:>9}{:>8}{:>13}".format("rule", "n", "avg$", "PF", "CI low")
          + "".join("{:>15}".format(f"{a}-{b}") for a, b in buckets))
    keep = {}
    for nm, rule in SHORTS.items():
        tr = []
        for d in bars.values():
            tr += sim(d, rule, True, regime)
        keep[nm] = tr
        s = stats([p for _, p, _ in tr])
        if not s:
            print("{:<24} insufficient".format(nm))
            continue
        row = "{:<24}{:>7d}{:>9,.0f}{:>8.2f}{:>13,.0f}".format(
            nm, s["n"], s["avg"], s["pf"], s["lo"])
        for a, b in buckets:
            d_ = stats([p for y, p, _ in tr if a <= y <= b])
            row += "{:>15}".format(f"{d_['avg']:+,.0f}({d_['n']})" if d_ else "--")
        print(row, flush=True)

    print("\n" + "=" * 104)
    print("BY REGIME   (a short book that only works in drawdowns is still worth having)")
    print("=" * 104)
    print("{:<24}{:>24}{:>24}{:>24}".format("rule", "BULL", "BEAR", "HIGH-VOL"))
    for nm, tr in keep.items():
        row = "{:<24}".format(nm)
        for rg in ("BULL", "BEAR", "HIGH-VOL"):
            s = stats([p for _, p, r in tr if r == rg])
            row += "{:>24}".format(
                f"{s['avg']:+,.0f} | PF {s['pf']:.2f} ({s['n']})" if s else "--")
        print(row, flush=True)

    print("\n" + "=" * 104)
    print("VERDICT")
    print("=" * 104)
    for nm, tr in keep.items():
        bear = stats([p for _, p, r in tr if r in ("BEAR", "HIGH-VOL")])
        alls = stats([p for _, p, _ in tr])
        if not alls:
            continue
        if alls["lo"] > 0:
            v = "PROFITABLE overall even with survivorship bias against it"
        elif bear and bear["lo"] > 0:
            v = "profitable in BEAR/HIGH-VOL only — use as a regime hedge"
        elif alls["avg"] > 0:
            v = "positive but not significant — inconclusive"
        else:
            v = "negative here; bias means this is not conclusive"
        print(f"  {nm:<24} {v}")


if __name__ == "__main__":
    main()
