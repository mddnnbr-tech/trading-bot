# What Cowork Needs From Baker — Access Checklist

Everything below is required before the system can go fully live.
Items are ordered: easiest first.

---

## ✅ Already Have / Already Done
- E*TRADE sandbox credentials (in `.env` on your Windows machine)
- GitHub repo `tading-bot` (code lives here)
- Python environment set up on Windows (VS Code, all packages installed)
- Agent scripts built: TechnicalAgent, NewsAgent, SentimentAgent, RiskAgent, MetaAgent

---

## 🔑 Still Needed From You

### 1. Gmail App Password — 5 minutes
Needed for: weekly performance email sent to you every Monday

Steps:
1. Go to https://myaccount.google.com/security
2. Enable 2-Step Verification if not already on
3. Go to App Passwords → Generate one for "Mail / Other (Trading Bot)"
4. Copy the 16-character code
5. Add to your `.env` file:
   ```
   GMAIL_ADDRESS=mddnnbr@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   REPORT_TO_EMAIL=mddnnbr@gmail.com
   ```

---

### 2. Google Cloud Account — 15–20 minutes
Needed for: running the trading bot 24/7 even when your PC is off

Steps: Follow CLOUD_SETUP.md (included in this folder) — it walks you through every click.

What you'll set up:
- Free Google Cloud account (always-free e2-micro VM)
- Bot uploaded and running as a background service
- Weekly cron job for email reports

No ongoing cost as long as you use an e2-micro VM in us-east1/us-central1/us-west1.

---

### 3. Share Your GitHub Repo With Cowork — Optional but helpful
Needed for: Cowork being able to read your actual agent code to give better recommendations

Option A — Add Cowork as a collaborator:
- Go to github.com/mddnnbr-tech/tading-bot → Settings → Collaborators
- Invite: (share the repo URL with Cowork in a session and Cowork can read it via the files you upload)

Option B — Upload files directly to Cowork:
- In any Cowork session, attach the Python files you want reviewed
- Cowork can read and analyze them immediately

---

### 4. E*TRADE Production Credentials — When Ready for Phase F (Go-Live)
Needed for: live trading (not paper trading)

These are separate from your sandbox credentials. You already have sandbox set up.
Production credentials come from E*TRADE Developer when you're ready for live trading.
Do NOT enter these until you've completed 2–4 weeks of paper trading (Phase D).

---

## 📋 Summary Table

| Item | Purpose | Time to Set Up | Urgency |
|------|---------|---------------|---------|
| Gmail App Password | Weekly email report | 5 min | High — needed now |
| Google Cloud VM | 24/7 bot operation | 20 min | High — needed now |
| GitHub access | Better code reviews | 5 min | Low — optional |
| E*TRADE production keys | Live trading | 10 min | Not yet — Phase F only |

---

## 🗓 Scheduled Tasks Now Active in Cowork

These are already set up and will run automatically on your PC:

| Task | Schedule | What It Does |
|------|----------|-------------|
| `trading-agent-review-morning` | Weekdays 10:00 AM | Cowork reads logs, reports on agent P&L |
| `trading-agent-review-afternoon` | Weekdays 3:30 PM | Cowork end-of-day review + rotation check |
| `trading-weekly-email-report` | Mondays 8:00 AM | Runs weekly_reporter.py, sends email to you |

Note: The Cowork reviews require your PC to be on. The cloud VM handles the actual trading independently.
