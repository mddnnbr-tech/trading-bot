# v2.2 Upload Guide — Trade Ledger + Rebuilt Daily Report

**Date:** 2026-04-24
**Goal:** Replace the empty 0/0 paper P&L numbers with a real ledger-backed
report that includes Daily Trends, Portfolio Since Inception, Open Positions,
and a Per-Agent Evaluator.

---

## What's new

Two files are new/changed in your workspace:

| File | Status | Purpose |
|------|--------|---------|
| `trade_ledger.py` | **NEW** | Parses `scheduler.log` PAPER TRADE lines into `data/paper_trades.csv`, fetches current prices, marks target/stop hits, exposes query API. |
| `daily_reporter.py` | **MODIFIED** (v2.2) | Reads from the new ledger. Adds 4 new email sections. Backward-compatible: if `trade_ledger.py` is missing, falls back to v2.1 behavior + shows a warning banner. |

**Preview the new email layout** before uploading:
[View PREVIEW_daily_report_v2.2.html](computer:///sessions/practical-gracious-knuth/mnt/Automated%20Trading/PREVIEW_daily_report_v2.2.html)
*(This was rendered with simulated trades so you can see how every section looks when populated. Numbers are fake.)*

---

## Upload steps (copy/paste into SSH-in-browser)

### Step 1 — Upload both files

In SSH-in-browser, click **UPLOAD FILE** (top-right) and pick:
- `trade_ledger.py`
- `daily_reporter.py`

Files land in `~/` by default. Move them into the bot folder:

```bash
mv ~/trade_ledger.py ~/tading-bot/
mv ~/daily_reporter.py ~/tading-bot/
ls -la ~/tading-bot/trade_ledger.py ~/tading-bot/daily_reporter.py
```

### Step 2 — Backfill the ledger from scheduler.log

This is the magic step. It walks every `PAPER TRADE:` line in your log and
builds the structured CSV. Idempotent — safe to run any number of times.

```bash
cd ~/tading-bot && python3 trade_ledger.py
```

You should see something like:

```
[trade_ledger] parse_log: +47 new trades  (47 total in ledger)
```

That number is every paper trade your bot has logged since you started Phase D.

### Step 3 — Fetch current prices + mark target/stop hits

```bash
cd ~/tading-bot && python3 trade_ledger.py --refresh-prices
```

This pulls intraday bars for every open position and marks any that already
hit their target or stop. Trades older than 5 trading days auto-close at last
price. Output looks like:

```
[trade_ledger] {'checked': 47, 'closed_target': 8, 'closed_stop': 12,
                'expired': 3, 'still_open': 24}
```

### Step 4 — Quick CLI sanity check

```bash
cd ~/tading-bot && python3 trade_ledger.py --summary
```

Prints cumulative P&L, agent leaderboard, top performers. If this looks
right, the email will be right.

### Step 5 — Send a fresh report email NOW

```bash
cd ~/tading-bot && python3 daily_reporter.py --send-now
```

Check your inbox. The subject is **"Trading Bot — Daily Report v2 (Apr 24, 2026)"**.

Look for these new sections (in this order):
1. 💰 **Performance Tracking — Paper vs. Live** (now populated, not 0/0)
2. 📒 **Ledger banner** (green, shows trade count + refresh stats)
3. 📈 **Daily Trends** (today vs yesterday vs 5-day avg + 7-day table)
4. 🧮 **Portfolio Since Inception** (total P&L, win rate, best/worst day)
5. 📂 **Currently Open Positions** (every open trade with target/stop/unrealized)
6. 🤖 **Per-Agent Evaluator** (every agent ranked by P&L, with best/worst trade)

---

## Optional: add ledger refresh to cron (recommended)

Right now `daily_reporter.py` calls `parse_log` and `refresh_open_positions`
every time it runs, so the 4:35 PM cron run does it automatically. But if you
want the ledger updated more frequently (e.g. for an intraday dashboard later),
add a second cron line:

```bash
crontab -e
```

Append this line:

```
*/30 9-16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/trade_ledger.py --refresh-prices >> /home/mddnnbr/tading-bot/logs/ledger_cron.log 2>&1
```

That refreshes the ledger every 30 minutes during market hours on weekdays.

---

## Troubleshooting

**"trade_ledger module not loaded" warning in email**
→ trade_ledger.py isn't next to daily_reporter.py. Re-run Step 1.

**"+0 new trades" in Step 2**
→ Either scheduler.log has no PAPER TRADE lines (check with
`grep "PAPER TRADE" ~/tading-bot/logs/scheduler.log | wc -l`),
or the ledger already has them all (idempotent — this is normal on re-runs).

**Refresh fails with "yfinance not installed"**
→ `pip install yfinance --break-system-packages`

**Email still shows 0/0 in the Paper column after upload**
→ Run Step 4 and verify cumulative P&L is non-zero. If it is non-zero in CLI
but zero in email, the import path is wrong — check that both files are in
the same directory: `ls -la ~/tading-bot/{trade_ledger,daily_reporter}.py`

---

## What this does NOT do (intentionally)

- **Live trading execution** — still Phase B, the LIVE column stays in standby.
- **Weekly rollup email** — deferred. Once daily v2.2 is stable for a few days,
  we'll restore `weekly_reporter.py` to read from the same ledger and produce
  a true Friday rollup.
- **Bot-side write hook** — we're parsing `scheduler.log` retroactively rather
  than modifying the bot to write trades directly. Easier deploy, no risk to
  the trading engine. Can revisit later if log parsing ever drifts.

---

## Rollback

If anything breaks the email, instant rollback:

```bash
cd ~/tading-bot && rm trade_ledger.py
```

That alone reverts to v2.1 behavior — `daily_reporter.py` will detect the
missing module, show the warning banner at the top of the email, and the
old shadow_pnl path takes over. No further changes needed.
