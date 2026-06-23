# VM Update Guide — June 2026 Major Fix
## What Was Fixed (Run This Immediately)

### Problems That Were Found
1. **9 agent files were MISSING** — the ensemble imports files that didn't exist, which is why trading crashed
2. **`ensemble.py` didn't exist** — the scheduler imports `ensemble.py` but only `ensemble_v11.py` existed
3. **No Alpaca real-time streaming** — all data was daily/5-min delayed yfinance
4. **No learning mechanism** — agents never improved from experience
5. **agent_summary.json only tracked 5 of 12 agents**

### New Files Created (all must be uploaded to VM)
```
ensemble.py              ← CRITICAL: replaces ensemble_v11.py
regime_detector.py       ← was missing
momentum_agent.py        ← was missing
breakout_agent.py        ← was missing
bearish_pattern_agent.py ← was missing
short_momentum_agent.py  ← was missing
earnings_agent.py        ← was missing
macro_agent.py           ← was missing
premarket_agent.py       ← was missing
sector_rotation_agent.py ← was missing
options_flow_agent.py    ← was missing
alpaca_stream.py         ← NEW: real-time Alpaca WebSocket data
strategy_learner.py      ← NEW: weekly learning/self-improvement
```

### Updated Files (must be re-uploaded)
```
market_scheduler.py      ← adds learning run + ledger refresh every 5 min
logs/agent_summary.json  ← now tracks all 13 agents
.env                     ← added ALPACA_API_KEY / ALPACA_API_SECRET fields
```

---

## Step 1 — SSH into Your VM

Open Chrome → go to: https://console.cloud.google.com/compute/instances
Click SSH next to your `trading-bot` VM.

---

## Step 2 — Stop the Running Bot

```bash
sudo systemctl stop trading-bot
sudo systemctl status trading-bot   # confirm it says "inactive"
```

---

## Step 3 — Check What's Currently on the VM

```bash
ls -la ~/tading-bot/
cat ~/tading-bot/logs/scheduler.log | tail -50
```

Look at the last 50 log lines to see WHY it stopped. It likely shows an ImportError.

---

## Step 4 — Pull from GitHub (if you pushed there)

If your code is synced to https://github.com/mddnnbr-tech/tading-bot :

```bash
cd ~/tading-bot
git pull origin main
```

If git pull works, skip to Step 6.

---

## Step 5 — Manual Upload (if not using GitHub)

In the SSH window, click the gear icon (⚙) → Upload file.
Upload EACH of these files from your local folder:
`C:\Users\ashle\OneDrive\Documents\Claude\Projects\Automated Trading\`

Upload in this order:
1. ensemble.py
2. regime_detector.py
3. momentum_agent.py
4. breakout_agent.py
5. bearish_pattern_agent.py
6. short_momentum_agent.py
7. earnings_agent.py
8. macro_agent.py
9. premarket_agent.py
10. sector_rotation_agent.py
11. options_flow_agent.py
12. alpaca_stream.py
13. strategy_learner.py
14. market_scheduler.py
15. logs/agent_summary.json  ← upload to the logs/ subfolder

---

## Step 6 — Install New Dependencies

```bash
cd ~/tading-bot
pip3 install alpaca-py --break-system-packages
pip3 install yfinance pandas --upgrade --break-system-packages
```

---

## Step 7 — Add Your Alpaca API Keys to .env

Get free Alpaca keys:
1. Go to https://alpaca.markets → sign up (free)
2. Go to Paper Trading → Settings → API Keys → Generate new
3. Copy Key ID and Secret Key

```bash
nano ~/tading-bot/.env
```

Add these lines (fill in your actual keys):
```
ALPACA_API_KEY=your_key_id_here
ALPACA_API_SECRET=your_secret_key_here
ALPACA_PAPER=true
```

Press Ctrl+X → Y → Enter to save.

**Also add your Anthropic API key** (get from https://console.anthropic.com):
```
ANTHROPIC_API_KEY=your_anthropic_key_here
```

---

## Step 8 — Test the System

```bash
cd ~/tading-bot

# Quick test: does ensemble import without errors?
python3 -c "from ensemble import run_ensemble; print('OK')"

# Test individual new agents
python3 momentum_agent.py
python3 regime_detector.py
python3 sector_rotation_agent.py

# Full ensemble test (runs one cycle)
python3 ensemble.py
```

You should see signals being generated and "PAPER TRADE: ..." lines in the output.

---

## Step 9 — Restart the Bot

```bash
sudo systemctl start trading-bot
sudo systemctl status trading-bot   # should say "active (running)"

# Watch live logs
tail -f ~/tading-bot/logs/scheduler.log
```

---

## Step 10 — Verify Trades Are Being Recorded

After the market opens (9:30 AM ET) and the first few minutes pass:

```bash
# Check if paper trades are being logged
grep "PAPER TRADE" ~/tading-bot/logs/scheduler.log | tail -20

# After trades: parse them into the ledger
python3 trade_ledger.py --summary

# Check the ledger file
ls -la ~/tading-bot/data/
cat ~/tading-bot/data/paper_trades.csv
```

---

## What You'll See When It's Working

In `scheduler.log`:
```
[INFO] AlpacaStream: connected — receiving live data
[INFO] RegimeDetector: SPY=$560.25 RSI=58.3 VIX=16.2 → {'BULL_TREND', 'LOW_VOL'}
[INFO] TechnicalAgent: 2 signal(s)
[INFO] MomentumAgent: 1 signal(s)
[INFO] BreakoutAgent: 1 signal(s)
[INFO] ✅ APPROVED: NVDA   long  conf=0.74 tier=starter
[INFO] 📋 PAPER TRADE: NVDA LONG entry=$950.00 target=$997.50 stop=$926.25 agent=MetaAgent(MomentumAgent, BreakoutAgent)
```

The MetaAgent will reward agents that produce winners — their signals will get higher weights in future cycles.

---

## Weekly Learning (Automatic)

Every Friday at 3:45 PM ET, the system now runs `strategy_learner.py` automatically.
It analyzes all closed trades and adjusts:
- Which agents are generating the best signals (raises/lowers their confidence threshold)
- Which symbols to avoid (poor historical win rate)
- Best time-of-day for each agent

Results are saved to `logs/learned_params.json`.

---

## Getting Your API Keys

| Service | URL | Why You Need It |
|---------|-----|-----------------|
| Alpaca | https://alpaca.markets | Real-time streaming data |
| Anthropic | https://console.anthropic.com | Claude AI agents (optional) |
| Alpaca is FREE | No credit card for paper trading | Priority — get this first |
