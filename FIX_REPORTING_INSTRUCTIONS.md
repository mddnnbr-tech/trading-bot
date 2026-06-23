# Fix Reporting + Switch to Daily Report — Step by Step

Follow these in order. Total time: ~10 minutes once SSH is back.

---

## Step 1 — Get SSH working again

After a GCE reboot, the SSH-in-browser button often fails for 2–4 minutes while the instance finishes booting and the `google-guest-agent` / OpenSSH daemon re-initializes. Steps:

1. **Wait 3 full minutes** after pressing "Restart" before retrying. GCE sometimes accepts connections before the keys are re-provisioned, which causes silent timeouts.
2. In the **GCE Console → VM instances**, confirm the `trading-bot` instance shows the green checkmark (status: RUNNING).
3. Click **SSH ▾ → Open in browser window** (not the dropdown item — the button). If it still times out:
   - Click **SSH ▾ → View gcloud command**, copy the command, and run it in **Cloud Shell** (the `>_` icon in the top nav bar). Cloud Shell goes through IAP and bypasses whatever browser SSH handshake is failing.
4. If Cloud Shell also fails, check the **Serial console** (VM instance → Logs → Serial port 1). You're looking for `Started OpenSSH server daemon` near the bottom. If it's not there, the SSH daemon never started — reboot once more from the console.

Once you're in, run:

```
uptime
systemctl status trading-bot
```

`uptime` confirms when the VM came back. `systemctl status trading-bot` tells us whether the bot came back up with it.

---

## Step 2 — Find out if trades happened today

Run these four commands on the VM and paste me the output:

```
date
grep "$(date +%Y-%m-%d)" ~/tading-bot/logs/scheduler.log | tail -20
grep "$(date +%Y-%m-%d)" ~/tading-bot/logs/trade_log.jsonl | wc -l
grep "$(date +%Y-%m-%d)" ~/tading-bot/logs/trade_log.jsonl | grep approved | wc -l
cat ~/tading-bot/logs/agent_summary.json
```

Translation:
- Line 1: confirms VM timezone is ET.
- Line 2: scheduler heartbeat — if nothing prints, the scheduler never ticked today.
- Line 3: total signals generated today (approved + rejected).
- Line 4: signals that made it through the risk bridge.
- Line 5: running per-agent P&L / state.

**If line 3 is 0 and line 2 is empty**, the bot wasn't running today (scheduler never ticked). Likely because the restart killed the process and it didn't come back up. Fix:

```
sudo systemctl start trading-bot
sudo systemctl enable trading-bot       # ensure it auto-starts next reboot
systemctl status trading-bot
```

**If line 3 is > 0**, trades (or at least signals) happened today — the email is the thing that broke, not the bot.

---

## Step 3 — Find out why today's email didn't arrive

On the VM:

```
crontab -l
grep CRON /var/log/syslog | grep "$(date +%b\ %e)" | tail -20
```

- `crontab -l` lists every scheduled email job. If you don't see both `send_recap_email.py` AND `weekly_reporter.py` here, the cron entry got lost (likely after the reboot/restart).
- The syslog grep shows whether cron actually fired today — if there's no output, cron itself isn't running.

Then test the Gmail credentials manually:

```
cd ~/tading-bot
python3 send_recap_email.py
```

If this succeeds, you'll get the old-format daily email within 30 seconds. If it errors on `GMAIL_APP_PASSWORD` or throws an SMTP auth error, the app password was revoked (Google sometimes auto-revokes after a security event). Generate a new one at https://myaccount.google.com/apppasswords and replace the value in `~/tading-bot/.env`.

---

## Step 4 — Install the new daily reporter (weekly format, daily scope)

I wrote `daily_reporter.py` — it reuses the weekly report's HTML styling but scopes everything to today, AND pulls from both the signal log AND the closed-trade ledger (so you won't hit the same "approved signals but empty report" disconnect that bit you today).

**New KPIs vs. the old plain-text recap:**

- Today's Net P&L (closed trades only)
- Signals: total / approved / rejected
- Closed trades count + W/L
- Per-agent table (P&L, signals, closed, win%)
- Best/worst trade of the day
- **Open positions table** (this is what was missing — explains why the weekly showed zero)
- SPY/QQQ intraday comparison
- Risk rejection reason breakdown
- All-time running totals

**Upload it to the VM:**

In GCE browser SSH: gear icon (top right) → **Upload file** → select `daily_reporter.py` from the folder I just saved it in. Then:

```
mv ~/daily_reporter.py ~/tading-bot/daily_reporter.py
chmod +x ~/tading-bot/daily_reporter.py
cd ~/tading-bot
python3 daily_reporter.py --send-now
```

You should get an email within a minute in the new format. If it errors, paste me the traceback.

---

## Step 5 — Swap cron: add daily, remove weekly

```
crontab -e
```

**Remove this line** (Monday weekly report):
```
0 7 * * 1  /usr/bin/python3 /home/mddnnbr/tading-bot/weekly_reporter.py --send-now
```

**Remove the old plain-text daily recap** (if it's there):
```
30 16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/send_recap_email.py
```

**Add this line** (new daily report, 4:35 PM ET Mon–Fri — runs 5 min after close so closed-position data is written):
```
35 16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/daily_reporter.py --send-now
```

Save and exit (Ctrl+O, Enter, Ctrl+X in nano). Verify:

```
crontab -l
```

Should show exactly one email-related line: the daily reporter at 35 16 Mon–Fri.

---

## Step 6 — Make it survive the next reboot

The reason you didn't get today's email is almost certainly that the reboot killed something that wasn't set to auto-start. Lock this down:

```
sudo systemctl enable trading-bot
sudo systemctl enable cron
sudo systemctl status trading-bot cron
```

Both should show `enabled; vendor preset: enabled`.

---

## About Friday's "trades" vs. the empty weekly

The weekly report reads `PerformanceLogger.get_trades()` — which only counts trades that have **closed with realized P&L**. Friday's approved signals either:

- Opened positions that are still open (so no closed-trade record yet), or
- Never actually executed on a ledger (Phase B / order execution module isn't built yet per your project notes)

The new `daily_reporter.py` now shows **signals AND closed trades AND open positions** separately, so you'll see all three numbers and can tell at a glance which is which. That's the structural fix — the old report was conflating them.

Once Phase B is built out, closed trades will start populating and the P&L line will come alive. Until then, the "Signals (✓/✗)" and "Open Positions" sections are where the real activity shows.

---

## TL;DR checklist

- [ ] Wait 3 min, retry SSH — fall back to Cloud Shell
- [ ] Run the five diagnostic commands → confirm if trades happened today
- [ ] Restart `trading-bot` service if it's down, `systemctl enable` it
- [ ] Test `send_recap_email.py` manually — fixes Gmail app password if needed
- [ ] Upload `daily_reporter.py` to `~/tading-bot/`
- [ ] Test with `python3 daily_reporter.py --send-now`
- [ ] Update crontab: remove weekly + old daily, add new daily
- [ ] `systemctl enable` both `trading-bot` and `cron`
