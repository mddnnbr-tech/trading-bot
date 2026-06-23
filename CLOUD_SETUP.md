# Cloud Setup Guide — Run the Trading Bot 24/7
### Google Cloud VM (Always-Free e2-micro) — Step-by-Step

This guide gets your trading bot running on a cloud server so it works during market hours even when your Windows PC is off or sleeping. The Google Cloud e2-micro VM is **permanently free** (no expiry, no credit card charges as long as you stay within the free tier).

---

## Part 1 — Create a Google Cloud Account

1. Go to **https://cloud.google.com** and click **Get started for free**
2. Sign in with your Google account (your Gmail)
3. Enter billing info when asked — Google requires a card to verify identity, but **you will not be charged** for an e2-micro VM in us-east1/us-west1/us-central1
4. You'll land on the Google Cloud Console dashboard

---

## Part 2 — Create Your Free VM

1. In the top search bar, type **Compute Engine** and click it
2. Click **Create Instance**
3. Fill in these fields exactly:

   | Field | Value |
   |-------|-------|
   | Name | `trading-bot` |
   | Region | `us-east1` (or `us-central1` or `us-west1`) |
   | Zone | any in that region |
   | Machine type | **e2-micro** (under "General purpose") |
   | Boot disk | Debian GNU/Linux 12, 10 GB standard |
   | Firewall | leave both unchecked |

4. Click **Create** — wait ~60 seconds for it to start
5. You'll see your VM in the list with a green checkmark

---

## Part 3 — Connect to Your VM

In the Compute Engine instance list, click **SSH** next to your `trading-bot` VM. A browser terminal window will open. You are now inside your cloud server.

---

## Part 4 — Install Python and Dependencies

Paste these commands one at a time into the SSH window:

```bash
# Update the system
sudo apt-get update && sudo apt-get upgrade -y

# Install Python 3, pip, and git
sudo apt-get install -y python3 python3-pip git

# Install the trading bot's Python packages
pip3 install requests requests-oauthlib python-dotenv pandas_market_calendars --break-system-packages
```

---

## Part 5 — Upload Your Bot Files to the VM

### Option A — From GitHub (recommended)
If your code is in the `tading-bot` GitHub repo:

```bash
git clone https://github.com/mddnnbr-tech/tading-bot.git
cd tading-bot
```

### Option B — Manual upload
In the SSH window, click the **gear icon (⚙)** → **Upload file** and upload:
- `performance_logger.py`
- `agent_evaluator.py`
- `agent_rotator.py`
- `market_scheduler.py`
- `weekly_reporter.py`
- `.env` (your credentials file)

---

## Part 6 — Create the .env File on the VM

In the SSH window, type:

```bash
nano .env
```

Paste in your credentials (replace the placeholder values):

```
# E*TRADE API
ETRADE_CONSUMER_KEY=your_consumer_key
ETRADE_CONSUMER_SECRET=your_consumer_secret
ETRADE_ACCESS_TOKEN=your_access_token
ETRADE_ACCESS_TOKEN_SECRET=your_access_token_secret

# Gmail (for weekly report)
GMAIL_ADDRESS=mddnnbr@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
REPORT_TO_EMAIL=mddnnbr@gmail.com
```

Press **Ctrl+X**, then **Y**, then **Enter** to save.

### How to get your Gmail App Password:
1. Go to **https://myaccount.google.com/security**
2. Under "How you sign in to Google," click **2-Step Verification** (must be enabled)
3. Scroll to the bottom → click **App passwords**
4. Select app: **Mail** | Select device: **Other** → type "Trading Bot" → click **Generate**
5. Copy the 16-character password shown (formatted as `xxxx xxxx xxxx xxxx`)
6. Paste it into your `.env` file as `GMAIL_APP_PASSWORD`

---

## Part 7 — Create a Logs Directory

```bash
mkdir -p logs
```

---

## Part 8 — Run a Quick Test

```bash
python3 agent_evaluator.py
```

You should see an evaluation table printed (all agents at $0 until trades are logged). If it runs without errors, you're good.

---

## Part 9 — Set Up Auto-Start with systemd

This makes the bot restart automatically if the VM reboots.

**Step 1** — Create the service file:

```bash
sudo nano /etc/systemd/system/trading-bot.service
```

**Step 2** — Paste this content:

```ini
[Unit]
Description=Trading Bot Market Scheduler
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=mddnnbr
WorkingDirectory=/home/mddnnbr/tading-bot
ExecStart=/usr/bin/python3 /home/mddnnbr/tading-bot/market_scheduler.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Press **Ctrl+X** → **Y** → **Enter** to save.

**Step 3** — Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
```

**Step 4** — Verify it's running:

```bash
sudo systemctl status trading-bot
```

You should see `Active: active (running)`.

---

## Part 10 — Set Up Email Cron Jobs (Daily Recap + Weekly Report)

```bash
crontab -e
```

If asked which editor, type `1` (nano). Add **both** lines at the bottom:

```
# Daily recap email — weekdays at 4:30 PM ET (after market close)
30 16 * * 1-5 /usr/bin/python3 /home/mddnnbr/tading-bot/send_recap_email.py >> /home/mddnnbr/tading-bot/logs/email_sender.log 2>&1

# Weekly HTML performance report — every Monday at 7:00 AM ET
0 7 * * 1 /usr/bin/python3 /home/mddnnbr/tading-bot/weekly_reporter.py --send-now >> /home/mddnnbr/tading-bot/logs/email_sender.log 2>&1
```

Press **Ctrl+X** → **Y** → **Enter**.

Make sure the VM is in Eastern Time so the schedule aligns with market hours:

```bash
sudo timedatectl set-timezone America/New_York
timedatectl   # confirm it shows ET
```

---

## Part 11 — Check the Logs Anytime

```bash
# Live scheduler log
tail -f logs/scheduler.log

# Recent trades
tail -20 logs/trade_log.jsonl

# Latest evaluation
cat logs/latest_eval.json

# Rotation history
cat logs/rotation_log.jsonl
```

---

## Keeping the VM Free

The e2-micro free tier includes:
- 1 VM in us-east1, us-central1, or us-west1
- 30 GB standard storage
- 1 GB network egress/month

Your trading bot is well within these limits. To confirm you're on track:
1. Go to **Billing** → **Reports** in the Cloud Console
2. Current charges should show **$0.00**

---

## Summary — What's Running Where

| Component | Where it runs |
|-----------|--------------|
| Trading agents (TechnicalAgent, NewsAgent, etc.) | Google Cloud VM (24/7, market hours only) |
| Performance logging | Google Cloud VM → `logs/` folder |
| Agent evaluation + rotation | Google Cloud VM (10 AM + 3:30 PM ET) |
| Weekly email report | Google Cloud VM (Monday 7 AM ET) |
| Cowork twice-daily review | Your Windows PC via Cowork scheduled tasks |
| Weekly email draft review | Your Windows PC via Cowork scheduled tasks |

---

## Current Status (as of April 2026)

✅ VM created and running (`trading-bot`, us-central1-f)  
✅ Python + all packages installed  
✅ All bot files uploaded to `/home/mddnnbr/tading-bot/`  
✅ systemd service running — bot auto-starts on reboot  
✅ Phase C wired — all 3 agents feed into MetaAgent → ensemble  
✅ Paper trading mode active (`PAPER_TRADING=true`)  
⬜ **Email cron jobs — still need to do Part 10** (one-time SSH step)  
⬜ Phase D — run 2–4 weeks of paper trading, review weekly reports  
⬜ Phase B/F — order execution module + E*TRADE production keys (when ready to go live)  

## Next Steps After Cloud Setup

1. **Complete Part 10** — SSH into the VM and set up email cron jobs so you receive daily recaps at 4:30 PM ET and weekly HTML reports every Monday at 7 AM ET
2. Watch the logs for the first week to confirm signals are firing and paper trades are being recorded
3. After 2–4 weeks, review weekly reports and decide if agents need tuning before enabling live trading
