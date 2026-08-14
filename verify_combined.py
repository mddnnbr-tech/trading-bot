"""
verify_combined.py
──────────────────
Confirms the COMBINED configuration and finds the optimum around it.

portfolio_backtest.py swept one dimension at a time. Three changes were
then deployed together — shorts-in-bear, 5% positions, net 85% — on the
strength of three individually large, same-direction effects. That is
exactly the inference that went wrong three times this week, so it gets
measured rather than assumed.

Also sweeps position size and daily cap AROUND the winner, because the
one-at-a-time sweep only ever tested 5/10/20% against the OLD short
policy. With shorts correctly gated the optimum may have moved.
"""
import sys, warnings
import numpy as np, yfinance as yf
warnings.filterwarnings("ignore")
from deep_research import UNIVERSE, prep
from portfolio_backtest import Config, run, summarize

def main():
    start = "2005-01-01"
    print(f"downloading from {start} ...", flush=True)
    raw = yf.download(UNIVERSE, start=start, interval="1d", group_by="ticker",
                      progress=False, auto_adjust=True, threads=True)
    bars = {}
    for s in UNIVERSE:
        try:
            d = raw[s].dropna().copy()
            if len(d) > 1200:
                bars[s] = prep(d)
        except Exception:
            pass
    spy = bars["SPY"]; dates = spy.index
    spy_ret = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[210]) - 1) * 100
    print(f"{len(bars)} symbols, {len(dates)} sessions, SPY {spy_ret:+.0f}%\n", flush=True)

    C = []
    # The exact configuration now live.
    C.append(Config("*DEPLOYED* bear-shorts/pos5/net85", 0.85, 2.0, 5, 5.0, shorts_only_in_bear=True))
    # Position size around the winner, with shorts correctly gated.
    for p in (3.0, 7.0, 10.0):
        C.append(Config(f"bear-shorts/pos{p:g}/net85", 0.85, 2.0, 5, p, shorts_only_in_bear=True))
    # Net gate around the winner.
    for n in (0.70, 1.00):
        C.append(Config(f"bear-shorts/pos5/net{n:.0%}", n, 2.0, 5, 5.0, shorts_only_in_bear=True))
    # Daily cap — never swept with shorts gated.
    for cap in (3, 10):
        C.append(Config(f"bear-shorts/pos5/net85/cap{cap}", 0.85, 2.0, cap, 5.0, shorts_only_in_bear=True))
    # Gross backstop.
    C.append(Config("bear-shorts/pos5/net85/gross2.5", 0.85, 2.5, 5, 5.0, shorts_only_in_bear=True))

    print("{:<38}{:>11}{:>8}{:>9}{:>8}{:>8}{:>7}{:>9}{:>8}".format(
        "config","final$","CAGR","maxDD","Sharpe","trades","win%","avgGross","avgNet"), flush=True)
    print("-"*106, flush=True)
    rows = []
    for cfg in C:
        s = summarize(cfg, run(cfg, bars, spy, dates), spy_ret)
        if s.get("n") == 0:
            print(f"{cfg.name:<38} insufficient", flush=True); continue
        rows.append(s)
        print("{:<38}{:>11,.0f}{:>7.1f}%{:>8.1f}%{:>8.2f}{:>8d}{:>6.0f}%{:>8.2f}x{:>7.0f}%".format(
            s["name"], s["final"], s["cagr"], s["dd"], s["sharpe"], s["trades"],
            s["win"], s["gross"], s["net"]*100), flush=True)

    if rows:
        b = max(rows, key=lambda x: x["sharpe"])
        d = next((r for r in rows if r["name"].startswith("*DEPLOYED*")), None)
        print(f"\nSPY buy-and-hold: {spy_ret:+.0f}%  (~10.9% CAGR, ~55% drawdown)")
        print(f"BEST Sharpe : {b['name']}  Sharpe {b['sharpe']:.2f}  "
              f"CAGR {b['cagr']:.1f}%  maxDD {b['dd']:.1f}%")
        if d:
            print(f"DEPLOYED    : Sharpe {d['sharpe']:.2f}  CAGR {d['cagr']:.1f}%  "
                  f"maxDD {d['dd']:.1f}%")
            if b["name"] != d["name"]:
                print(f"-> deployed config is NOT optimal; {b['name']} is better")
            else:
                print("-> deployed config IS the optimum in this sweep")

if __name__ == "__main__":
    main()
