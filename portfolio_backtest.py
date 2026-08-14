"""
portfolio_backtest.py
─────────────────────
Simulates the WHOLE BOOK over history — concurrent positions, capital
limits, exposure gates, daily entry caps — and produces an equity curve.

Why this had to exist. replay.py and signal_research.py measure one trade
in isolation: entry to exit, nothing else open. That is the right tool for
"is this signal any good" and it settled the ATR stop question with 1,900
trades. It is the WRONG tool for every portfolio question, and portfolio
questions are where the money has actually been lost:

    how much net long exposure is too much
    does a gross leverage cap help or just starve the book
    how many entries per day
    how large should one position be
    do the gates protect the account or deadlock it

Every one of those numbers in ensemble.py was set by argument, because
nothing could measure them. On 2026-08-14 that produced a live deadlock —
86 signals a tick, zero entries — from two individually reasonable gates
that could not both be satisfied. A trade-level harness cannot catch that
because the failure only exists at the book level.

What is faithful here:
    • the exact sizing formula from agent_risk_bridge
      (risk_budget/stop_distance, capped by notional %)
    • the ATR(14)x1.5 stop capped at 4%, as ensemble._normalize_geometry
    • the progressive trail ratchet from order_executor, tighten-only
    • entry gates: net long %, gross leverage, daily cap, one-per-symbol
    • equity marked daily, so gates see the same numbers production does

What is a proxy: the signal layer, as always. Rules are the ones
signal_research.py validated over 1990-2026, standing in for the agent
ensemble. Conclusions transfer to portfolio CONSTRUCTION, not to whether
the agents pick well.

Survivorship bias is unchanged and still inflates long results.
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ATR_MULT, STOP_CAP = 1.5, 0.04
RISK_PCT           = 0.5      # % of equity risked per trade
START_EQUITY       = 100_000.0
MAX_HOLD           = 30

from deep_research import UNIVERSE, prep  # noqa: E402


def trail_for(gain_pct: float) -> float:
    return 3.0 if gain_pct >= 15 else 4.0 if gain_pct >= 8 else 5.5 if gain_pct >= 4 else 8.0


@dataclass
class Position:
    symbol: str
    short: bool
    shares: float
    entry: float
    stop: float
    peak: float
    opened: int          # bar index
    trail: float = 8.0


@dataclass
class Config:
    name: str
    max_net_long: float      # fraction of equity
    max_gross: float         # multiple of equity
    daily_cap: int
    max_pos_pct: float       # notional cap, % of equity
    allow_shorts: bool = True
    shorts_only_in_bear: bool = False


@dataclass
class Result:
    equity: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    trades: int = 0
    wins: int = 0
    blocked_net: int = 0
    blocked_gross: int = 0
    blocked_cap: int = 0
    gross_hist: list = field(default_factory=list)
    net_hist: list = field(default_factory=list)


def signals_on(df: pd.DataFrame, i: int) -> str | None:
    """Validated rules from signal_research.py. Uses bars strictly before i."""
    if i < 210:
        return None
    p = df.iloc[i - 1]
    c, sma20, sma200 = p.Close, p.sma20, p.sma200
    if not np.isfinite(sma200) or not np.isfinite(p.rsi):
        return None
    # Longs: buy-the-dip inside an uptrend (PF 1.61-1.68, 19/22 years)
    if c > sma200:
        if c < p.bb_lo or p.rsi < 30:
            return "long"
        if c < sma20 and 35 < p.rsi < 50:
            return "long"
    # Shorts: weakness below trend (PF 1.24-1.26, and only in BEAR/HIGH_VOL)
    if c < sma200 and p.sma50 < sma200 and c < sma20 and p.rsi < 40:
        return "short"
    return None


def run(cfg: Config, bars: dict, spy: pd.DataFrame, dates: pd.DatetimeIndex) -> Result:
    eq = START_EQUITY
    cash = START_EQUITY
    pos: dict[str, Position] = {}
    r = Result()
    # SPY 200-day defines the regime the short gate keys on.
    spy_bull = (spy["Close"] > spy["sma200"]).reindex(dates).ffill()

    for di, d in enumerate(dates):
        if di < 210:
            continue
        # ── 1. mark, and exit anything that hit its stop ────────────────
        for sym in list(pos):
            p = pos[sym]
            df = bars[sym]
            if d not in df.index:
                continue
            row = df.loc[d]
            hi, lo = float(row.High), float(row.Low)
            hit = (hi >= p.stop) if p.short else (lo <= p.stop)
            aged = (di - p.opened) >= MAX_HOLD
            if hit or aged:
                px = p.stop if hit else float(row.Close)
                pnl = (p.entry - px) * p.shares if p.short else (px - p.entry) * p.shares
                cash += pnl
                r.trades += 1
                r.wins += 1 if pnl > 0 else 0
                del pos[sym]
                continue
            # ratchet the trail — tighten only, never loosen
            p.peak = min(p.peak, lo) if p.short else max(p.peak, hi)
            gain = ((p.entry / p.peak - 1) if p.short else (p.peak / p.entry - 1)) * 100
            t = trail_for(gain)
            if t < p.trail:
                p.trail = t
            ns = p.peak * (1 + p.trail / 100) if p.short else p.peak * (1 - p.trail / 100)
            p.stop = min(p.stop, ns) if p.short else max(p.stop, ns)

        # ── 2. mark the book ────────────────────────────────────────────
        gross = net = 0.0
        unreal = 0.0
        for sym, p in pos.items():
            df = bars[sym]
            px = float(df.loc[d].Close) if d in df.index else p.entry
            mv = px * p.shares * (-1 if p.short else 1)
            gross += abs(mv)
            net += mv
            unreal += (p.entry - px) * p.shares if p.short else (px - p.entry) * p.shares
        eq = cash + unreal
        if eq <= 0:
            r.equity.append(0.0); r.dates.append(d)
            break
        r.equity.append(eq); r.dates.append(d)
        r.gross_hist.append(gross / eq)
        r.net_hist.append(net / eq)

        # ── 3. entries, subject to the same gates production uses ───────
        gate_gross = gross / eq > cfg.max_gross
        gate_net = net / eq > cfg.max_net_long
        opened_today = 0
        if gate_gross:
            r.blocked_gross += 1
        if gate_net:
            r.blocked_net += 1

        cands = []
        for sym, df in bars.items():
            if sym in pos or sym == "SPY":
                continue
            if d not in df.index:
                continue
            i = df.index.get_loc(d)
            s = signals_on(df, i)
            if s is None:
                continue
            if s == "short" and not cfg.allow_shorts:
                continue
            if s == "short" and cfg.shorts_only_in_bear and bool(spy_bull.get(d, True)):
                continue
            # a long adds net exposure; a short reduces it
            if s == "long" and (gate_net or gate_gross):
                continue
            if s == "short" and gate_gross:
                continue
            cands.append((sym, s, i))

        for sym, side, i in cands:
            if opened_today >= cfg.daily_cap:
                r.blocked_cap += 1
                break
            df = bars[sym]
            if i + 1 >= len(df):
                continue
            atr = float(df.iloc[i - 1].atr)
            entry = float(df.iloc[i].Open)
            if not np.isfinite(atr) or atr <= 0 or entry <= 0:
                continue
            sd = min(max(ATR_MULT * atr, entry * 0.01), entry * STOP_CAP)
            shares = min(eq * RISK_PCT / 100 / sd, eq * cfg.max_pos_pct / 100 / entry)
            if shares < 1:
                continue
            short = side == "short"
            pos[sym] = Position(sym, short, shares, entry,
                                entry + sd if short else entry - sd,
                                entry, di)
            opened_today += 1
    return r


def summarize(cfg: Config, r: Result, spy_ret: float) -> dict:
    if len(r.equity) < 100:
        return {"name": cfg.name, "n": 0}
    e = np.array(r.equity)
    yrs = len(e) / 252
    cagr = (e[-1] / e[0]) ** (1 / yrs) - 1
    dd = float(((e - np.maximum.accumulate(e)) / np.maximum.accumulate(e)).min())
    rets = np.diff(e) / e[:-1]
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() else 0
    return {"name": cfg.name, "final": e[-1], "cagr": cagr * 100, "dd": dd * 100,
            "sharpe": sharpe, "trades": r.trades,
            "win": r.wins / r.trades * 100 if r.trades else 0,
            "gross": np.mean(r.gross_hist) if r.gross_hist else 0,
            "net": np.mean(r.net_hist) if r.net_hist else 0,
            "vs_spy": (e[-1] / e[0] - 1) * 100 - spy_ret}


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2005-01-01"
    print(f"downloading universe from {start} ...", flush=True)
    raw = yf.download(UNIVERSE, start=start, interval="1d", group_by="ticker",
                      progress=False, auto_adjust=True, threads=True)
    bars = {}
    for s in UNIVERSE:
        try:
            d = raw[s].dropna().copy()
            if len(d) > 1200:
                bars[s] = prep(d)
        except Exception:
            continue
    spy = bars.get("SPY")
    if spy is None:
        print("no SPY"); return
    dates = spy.index
    spy_ret = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[210]) - 1) * 100
    print(f"{len(bars)} symbols, {len(dates)} sessions, SPY {spy_ret:+.0f}%\n", flush=True)

    CONFIGS = [
        Config("LIVE TODAY (net70/gross2.0/cap5/pos10)", 0.70, 2.0, 5, 10.0),
        Config("pre-fix    (net85/gross1.6/cap5/pos10)", 0.85, 1.6, 5, 10.0),
        Config("net 50%    (net50/gross2.0/cap5/pos10)", 0.50, 2.0, 5, 10.0),
        Config("net 100%   (net100/gross2.0/cap5/pos10)", 1.00, 2.0, 5, 10.0),
        Config("no gates   (net999/gross99/cap99/pos10)", 9.99, 99.0, 99, 10.0),
        Config("small pos  (net70/gross2.0/cap5/pos5)", 0.70, 2.0, 5, 5.0),
        Config("big pos    (net70/gross2.0/cap5/pos20)", 0.70, 2.0, 5, 20.0),
        Config("long only  (net70/gross2.0/cap5/pos10)", 0.70, 2.0, 5, 10.0, allow_shorts=False),
        Config("shorts in bear only", 0.70, 2.0, 5, 10.0, shorts_only_in_bear=True),
    ]

    rows = []
    for cfg in CONFIGS:
        r = run(cfg, bars, spy, dates)
        s = summarize(cfg, r, spy_ret)
        rows.append((s, r))
        if s.get("n") == 0:
            print(f"{cfg.name:<42} insufficient"); continue
        print("{:<42}{:>11,.0f}{:>8.1f}%{:>9.1f}%{:>8.2f}{:>8d}{:>7.0f}%{:>8.2f}x{:>7.0f}%".format(
            s["name"], s["final"], s["cagr"], s["dd"], s["sharpe"],
            s["trades"], s["win"], s["gross"], s["net"] * 100), flush=True)

    print("\n{:<42}{:>11}{:>9}{:>10}{:>8}{:>8}{:>8}{:>9}{:>8}".format(
        "config", "final$", "CAGR", "maxDD", "Sharpe", "trades", "win%", "avgGross", "avgNet"))
    print(f"\nSPY buy-and-hold over the same window: {spy_ret:+.0f}%")
    best = max((s for s, _ in rows if s.get("n") != 0), key=lambda x: x["sharpe"])
    print(f"best risk-adjusted: {best['name']}  (Sharpe {best['sharpe']:.2f}, "
          f"CAGR {best['cagr']:.1f}%, maxDD {best['dd']:.1f}%)")


if __name__ == "__main__":
    main()
