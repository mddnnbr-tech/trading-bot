"""
edgar_study.py
──────────────
Event study of SEC 8-K filings: does a material-event disclosure predict the
move that follows, and which kinds?

Why EDGAR rather than a news archive. A backtest on general news is almost
always circular — most articles EXPLAIN a move after it happened ("shares
fell on margin concerns"), so a model trained on them predicts the past.
8-K filings do not have that problem:

  • timestamped to the second of SEC acceptance, not a crawl date
  • legally required for material events, filed promptly
  • causally PRIOR to media coverage — reporters write from the filing
  • structured item codes, so event TYPE is data rather than inference
  • free and complete back to 1993

This is the only point-in-time news source we can get without paying for a
vendor feed, and it is a cleaner one than most vendors sell.

No-lookahead rule. Entry is always the NEXT session's open after the
acceptance timestamp. A filing accepted at 16:05 ET trades the following
morning; one accepted at 09:00 ET also trades at that day's open only if
acceptance precedes it, otherwise the next. Daily bars cannot resolve
intraday sequencing, so the pessimistic choice is taken every time.

Abnormal return is the stock's move minus SPY's over the same window, so a
result cannot be manufactured by the market drifting up.

Interpreting output: the question is not whether an item code moves price —
earnings obviously do — but whether the move is PREDICTABLE IN DIRECTION
from the filing type alone. A large mean with a near-50% hit rate means the
event creates volatility, which is tradeable with options but not with a
directional stock position.
"""

from __future__ import annotations

import json
import time
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# SEC requires a descriptive User-Agent with contact info and <=10 req/sec.
UA = {"User-Agent": "trading-bot-research mddnnbr@gmail.com"}
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{:010d}.json"

ITEM_NAMES = {
    "1.01": "material agreement entered",
    "1.02": "material agreement terminated",
    "2.01": "acquisition/disposition completed",
    "2.02": "RESULTS OF OPERATIONS (earnings)",
    "2.03": "direct financial obligation created",
    "2.04": "obligation acceleration triggered",
    "2.05": "exit/disposal costs",
    "2.06": "MATERIAL IMPAIRMENT",
    "3.01": "DELISTING NOTICE / listing rule failure",
    "4.01": "auditor changed",
    "4.02": "NON-RELIANCE on prior financials",
    "5.01": "change in control",
    "5.02": "director/officer departure or election",
    "5.03": "articles/bylaws amended",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements & exhibits",
}

from deep_research import UNIVERSE  # noqa: E402


def cik_map(tickers: list[str]) -> dict[str, int]:
    r = requests.get(SEC_TICKERS, headers=UA, timeout=30)
    r.raise_for_status()
    want = {t.upper() for t in tickers}
    out = {}
    for row in r.json().values():
        t = str(row["ticker"]).upper()
        if t in want:
            out[t] = int(row["cik_str"])
    return out


def filings_for(cik: int) -> list[tuple[pd.Timestamp, list[str]]]:
    """Every 8-K: (acceptance timestamp UTC, item codes). Includes archives."""
    out = []

    def harvest(block):
        forms = block.get("form", [])
        accepts = block.get("acceptanceDateTime", [])
        items = block.get("items", [])
        for i, f in enumerate(forms):
            if f != "8-K":
                continue
            try:
                ts = pd.Timestamp(accepts[i])
            except Exception:
                continue
            codes = [c.strip() for c in (items[i] or "").split(",") if c.strip()]
            out.append((ts, codes))

    r = requests.get(SUBMISSIONS.format(cik), headers=UA, timeout=30)
    if r.status_code != 200:
        return out
    j = r.json()
    harvest(j.get("filings", {}).get("recent", {}))
    for extra in j.get("filings", {}).get("files", []):
        time.sleep(0.12)
        try:
            e = requests.get(f"https://data.sec.gov/submissions/{extra['name']}",
                             headers=UA, timeout=30)
            if e.status_code == 200:
                harvest(e.json())
        except Exception:
            continue
    return out


def main():
    print("mapping tickers to CIKs ...", flush=True)
    cm = cik_map(UNIVERSE)
    print(f"  resolved {len(cm)}/{len(UNIVERSE)}", flush=True)

    print("downloading price history ...", flush=True)
    raw = yf.download(UNIVERSE + ["SPY"], start="1994-01-01", interval="1d",
                      group_by="ticker", progress=False, auto_adjust=True)
    px = {}
    for s in set(UNIVERSE + ["SPY"]):
        try:
            d = raw[s].dropna()
            if len(d) > 500:
                px[s] = d
        except Exception:
            continue
    spy = px.get("SPY")
    if spy is None:
        print("no SPY — cannot compute abnormal returns")
        return
    # tz-naive index for comparison against filing dates
    for s in px:
        px[s] = px[s].tz_localize(None) if px[s].index.tz else px[s]
    spy = px["SPY"]

    def fwd(df, entry_i, days):
        if entry_i + days >= len(df):
            return None
        o = float(df["Open"].iloc[entry_i])
        c = float(df["Close"].iloc[entry_i + days])
        return (c / o - 1) * 100 if o else None

    by_item = defaultdict(list)
    total = 0
    for n, (tk, cik) in enumerate(sorted(cm.items()), 1):
        if tk not in px:
            continue
        df = px[tk]
        try:
            fl = filings_for(cik)
        except Exception as e:
            print(f"  {tk}: {str(e)[:50]}")
            continue
        time.sleep(0.12)
        idx = df.index
        for ts, codes in fl:
            t = ts.tz_convert("America/New_York").tz_localize(None) if ts.tz else ts
            # Next session whose OPEN is strictly after the acceptance moment.
            pos = idx.searchsorted(t.normalize() + pd.Timedelta(days=0))
            if t.hour >= 9:           # accepted at/after the open -> next day
                pos = idx.searchsorted(t.normalize() + pd.Timedelta(days=1))
            if pos >= len(idx) - 21:
                continue
            si = spy.index.searchsorted(idx[pos])
            if si >= len(spy.index) - 21:
                continue
            for horizon in (1, 5, 20):
                a = fwd(df, pos, horizon)
                b = fwd(spy, si, horizon)
                if a is None or b is None:
                    continue
                for c in (codes or ["(none)"]):
                    by_item[(c, horizon)].append(a - b)
            total += 1
        if n % 10 == 0:
            print(f"  {n}/{len(cm)} tickers, {total:,} filings", flush=True)

    print(f"\ntotal 8-K filings matched to prices: {total:,}\n", flush=True)
    print("=" * 108)
    print("ABNORMAL RETURN AFTER AN 8-K, BY ITEM CODE   (stock minus SPY, entry = next open)")
    print("=" * 108)
    print("{:<6}{:<38}{:>7}{:>22}{:>22}".format(
        "item", "meaning", "n", "1-day  (hit%)", "5-day  (hit%)"))
    print("-" * 108)

    rows = []
    for code in sorted({c for c, _ in by_item}):
        a1 = np.array(by_item.get((code, 1), []))
        a5 = np.array(by_item.get((code, 5), []))
        if len(a1) < 100:
            continue
        se1 = a1.std(ddof=1) / np.sqrt(len(a1))
        sig = abs(a1.mean()) > 1.96 * se1
        rows.append((abs(a1.mean()), code, len(a1), a1.mean(),
                     (a1 > 0).mean() * 100,
                     a5.mean() if len(a5) else np.nan,
                     (a5 > 0).mean() * 100 if len(a5) else np.nan, sig))
    for _, code, n, m1, h1, m5, h5, sig in sorted(rows, reverse=True):
        star = " *" if sig else ""
        print("{:<6}{:<38}{:>7d}{:>16}{:>6}{:>16}{:>6}{}".format(
            code, ITEM_NAMES.get(code, "")[:37], n,
            f"{m1:+.2f}%", f"{h1:.0f}%",
            f"{m5:+.2f}%" if np.isfinite(m5) else "--",
            f"{h5:.0f}%" if np.isfinite(h5) else "--", star))
    print("\n* = 1-day mean differs from zero at 95% confidence")
    print("A big mean with a ~50% hit rate = volatility, not direction:")
    print("tradeable with options, NOT with a directional stock position.")


if __name__ == "__main__":
    main()
