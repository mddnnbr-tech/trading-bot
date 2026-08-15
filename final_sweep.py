"""
final_sweep.py
──────────────
The verification sweep moved one dial at a time off the deployed config
and found three independent improvements: cap3 (Sharpe 0.85), net100
(0.78), and pos10 (CAGR 11.4%). None were tested TOGETHER.

That matters more than usual here because the three interact directly:
a bigger position with fewer daily slots is a different book from either
change alone, and pos10 trades return for drawdown (-35.1% vs -18.9%)
where cap3 improves both at once.

Also worth settling: pos10/cap3 is the only combination that could plausibly
beat SPY on RAW return (10.9% CAGR) as well as on risk-adjusted terms,
which is the stated goal — outperform in good and bad markets.
"""
import warnings
import yfinance as yf
warnings.filterwarnings("ignore")
from deep_research import UNIVERSE, prep
from portfolio_backtest import Config, run, summarize

def main():
    raw = yf.download(UNIVERSE, start="2005-01-01", interval="1d",
                      group_by="ticker", progress=False, auto_adjust=True, threads=True)
    bars = {}
    for s in UNIVERSE:
        try:
            d = raw[s].dropna().copy()
            if len(d) > 1200: bars[s] = prep(d)
        except Exception: pass
    spy = bars["SPY"]; dates = spy.index
    spy_ret = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[210]) - 1) * 100
    print(f"{len(bars)} symbols, SPY {spy_ret:+.0f}% (~10.9% CAGR, ~55% DD)\n", flush=True)

    C = [
        Config("cap3/net85/pos5  (deploying now)", 0.85, 2.0, 3, 5.0,  shorts_only_in_bear=True),
        Config("cap3/net100/pos5",                 1.00, 2.0, 3, 5.0,  shorts_only_in_bear=True),
        Config("cap3/net100/pos7",                 1.00, 2.0, 3, 7.0,  shorts_only_in_bear=True),
        Config("cap3/net100/pos10",                1.00, 2.0, 3, 10.0, shorts_only_in_bear=True),
        Config("cap3/net85/pos10",                 0.85, 2.0, 3, 10.0, shorts_only_in_bear=True),
        Config("cap2/net100/pos10",                1.00, 2.0, 2, 10.0, shorts_only_in_bear=True),
    ]
    print("{:<34}{:>11}{:>8}{:>9}{:>8}{:>8}{:>8}".format(
        "config","final$","CAGR","maxDD","Sharpe","trades","avgNet"), flush=True)
    print("-"*88, flush=True)
    rows=[]
    for cfg in C:
        s = summarize(cfg, run(cfg, bars, spy, dates), spy_ret)
        if s.get("n")==0: continue
        rows.append(s)
        print("{:<34}{:>11,.0f}{:>7.1f}%{:>8.1f}%{:>8.2f}{:>8d}{:>7.0f}%".format(
            s["name"],s["final"],s["cagr"],s["dd"],s["sharpe"],s["trades"],s["net"]*100), flush=True)
    if rows:
        bs = max(rows, key=lambda x: x["sharpe"])
        bc = max(rows, key=lambda x: x["cagr"])
        print(f"\nbest Sharpe: {bs['name']}  {bs['sharpe']:.2f}  CAGR {bs['cagr']:.1f}%  DD {bs['dd']:.1f}%")
        print(f"best CAGR  : {bc['name']}  {bc['cagr']:.1f}%  Sharpe {bc['sharpe']:.2f}  DD {bc['dd']:.1f}%")
        beat = [r for r in rows if r["cagr"] > 10.9 and r["dd"] > -55]
        print("beats SPY on BOTH return and drawdown: " +
              (", ".join(r["name"] for r in beat) if beat else "none"))

if __name__ == "__main__":
    main()
