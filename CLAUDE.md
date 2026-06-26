# BluSterling & Associates — Claude Standing Instructions

## Who I Am Working For
Michael Baker, Managing Member of BluSterling & Associates LLC (Texas LLC, TX SOS Doc No. 1445311370002).
I am authorized to act as a company agent — pushing code, managing deployments, drafting documents, and executing tasks on Michael's behalf.

## Communication Style
- Michael is busy M–Th (meetings). Friday is his action day. Family time on weekends.
- Keep responses short and direct. No lengthy summaries at the end.
- Slack notifications: daily summary only at 4 PM ET market close + critical errors only. No mid-day noise.
- Do not ask for confirmation on routine tasks — just act and report what was done.

## Company Infrastructure
- **LLC**: BluSterling & Associates LLC — Texas, Northwest Registered Agent
- **Alpaca Paper Trading**: PA3EZ46Z9UUC — $100,000 paper account
- **Alpaca Business Account**: Brokerage 293573418 — under manual review
- **Slack Workspace**: blusterlingassociates.slack.com
  - #general — company comms
  - #trading-alerts — bot signals (channel ID: C0BDCAE92QN)
  - #blustering-updates — LLC/legal updates (channel ID: C0BD8306WQ3)
- **GitHub**: https://github.com/mddnnbr-tech/trading-bot.git (public, branch: master)
- **Google Cloud VM**: trading-bot, us-central1-f, project-a43c6f96-913a-4fbd-a91
- **VM bot path**: /home/mddnnbr/tading-bot/ (intentional typo — "tading" not "trading")
- **Emails**: mddnnbr@gmail.com (primary), ashleybaker1030@gmail.com (Ashley/wife)

## Trading Bot
- 12-agent ensemble + MetaAgent on Google Cloud VM (e2-micro)
- systemd service: `trading-bot` — runs Mon–Fri 09:30–16:00 ET
- Auto-deploy service: `trading-bot-deploy` — pulls GitHub every 60s, restarts on new commits
- Push code changes to GitHub master → deploys to VM automatically within 60 seconds
- .env is NOT in GitHub (gitignored) — env changes require SSH to VM
- Paper trading only until Alpaca business account approved

## Key Files
- `market_scheduler.py` — main entry, ticks every 60s during market hours
- `ensemble.py` — orchestrates 12 agents, singleton pattern
- `alpaca_stream.py` — real-time WebSocket streaming (DataFeed.IEX for paper)
- `meta_agent.py` — weights agents by P&L and regime
- `agent_risk_bridge.py` — 7-gate validation, MIN_CONFIDENCE=0.55
- `strategy_learner.py` — self-improvement cycle every Friday 3:45 PM ET

## How to Deploy Changes
1. Edit files locally at `C:\Users\ashle\OneDrive\Documents\Claude\Projects\Automated Trading\`
2. `git add <files> && git commit -m "message" && git push origin master`
3. VM auto-deploys within 60 seconds — no SSH needed for code changes
4. For .env changes: SSH via console.cloud.google.com → paste command to VM

## Pending Items (update as resolved)
- [ ] Alpaca business account approval (applied June 20 — expect ~June 25-27)
- [ ] Northwest Form 401 filing completion (in progress with TX SOS)
- [ ] Claim free domain from Northwest Registered Agent
- [ ] Add EIN to Operating Agreement Article 7.3 (find in CP575 PDF)
- [ ] Daily Slack summary at 4 PM ET from bot (needs market_scheduler.py update)
- [ ] Regenerate GitHub token (current token was shared in chat — security)
