# Rotation Fix — Upload Guide (v3: 4-file deploy)

**Date:** 2026-04-24
**Goal:** Make the agent rotator actually work AND make MetaAgent stop giving losing agents equal say. Two-layer defense:
- **Hard layer (bench)** — rotator + ensemble: lossy agents get benched entirely.
- **Soft layer (weight)** — MetaAgent: even before bench, lossy agents get their signal confidence multiplied by 0.15 instead of 1.0.

---

## What's changing

Four files need to ship together:

| File | Status | Purpose |
|------|--------|---------|
| `agent_evaluator.py` | **REPLACED** (v2.0) | Reads from `trade_ledger.py` instead of empty `trade_log.jsonl`. Discovers agents dynamically. Excludes MetaAgent from ensemble avg. Handles negative-average ensembles correctly. |
| `agent_rotator.py` | **PATCHED** (v1.1) | Removed TechnicalAgent from `PROTECTED_AGENTS`. Same import surface, same `EvalReport` contract, same cron-able CLI. |
| `ensemble.py` | **PATCHED** (v1.1) | Reads `logs/agent_summary.json` each cycle and skips agents marked `active=False`. Without it, the rotator was just writing to disk while the ensemble kept calling everyone. |
| `meta_agent.py` | **PATCHED** (v1.1) | **NEW.** `_load_performance_weights()` now reads from `trade_ledger.py` instead of the always-empty PerformanceLogger — so winning agents actually get higher weight in synthesis (was silently broken since launch, every agent was at weight 1.0). |

**Verified on the VM (grep returned zero hits):** the original `ensemble.py` had no awareness of `agent_summary.json`, no `is_active` check, no `benched` filter. The original `meta_agent._load_performance_weights()` read from `PerformanceLogger.get_trades()` which always returned `[]`, so `max_pnl <= 0` triggered the `return dict(DEFAULT_WEIGHTS)` early-return — every agent got weight 1.0 forever. Both the bench loop AND the weighting loop were no-ops. These four files close both gaps.

**Smoke-tested:**
- Evaluator + rotator: 6/6 dry-run scenarios produced expected flags + rotations.
- Bench filter (ensemble.py): 6/6 unit tests pass — file missing ✓, malformed JSON ✓, partial agent list (your real-world state) ✓, post-rotation state ✓.
- MetaAgent weighting (meta_agent.py): 10/10 assertions pass on synthetic Baker-shaped ledger. MomentumAgent → 1.000, OptionsFlowAgent + TechnicalAgent → 0.150 (floor), NewsAgent → 0.757 (mid). No-track-record agents collapse to floor instead of getting unearned 1.0.

**Critical fail-safe properties:**
- **ensemble.py:** when `agent_summary.json` is missing OR an agent isn't listed in it OR the JSON is malformed, the patched ensemble defaults to **running the agent**. We only skip when there's an explicit `active: false`. Safe to deploy BEFORE any rotation has been run.
- **meta_agent.py:** when `trade_ledger` is empty OR import fails OR computation throws, falls back to `DEFAULT_WEIGHTS = 1.0` for everyone — same as today's broken-but-working state. Whole-ensemble-underwater windows collapse everyone to 0.15 (so MetaAgent stops amplifying anyone until somebody recovers).

---

## Step 1 — Upload all four files

In SSH-in-browser, click **⬆ UPLOAD FILE** and upload (one at a time if needed):
- `agent_evaluator.py` (about 14 KB)
- `agent_rotator.py` (about 10 KB)
- `ensemble.py` (about 11 KB)
- `meta_agent.py` (about 17 KB)

Then move them into the bot folder, with timestamped backups of all four originals so rollback is trivial:

```bash
TS=$(date +%Y%m%d_%H%M%S) && \
cp ~/tading-bot/agent_evaluator.py ~/tading-bot/agent_evaluator.py.v1.backup_$TS && \
cp ~/tading-bot/agent_rotator.py   ~/tading-bot/agent_rotator.py.v1.backup_$TS && \
cp ~/tading-bot/ensemble.py        ~/tading-bot/ensemble.py.v1.backup_$TS && \
cp ~/tading-bot/meta_agent.py      ~/tading-bot/meta_agent.py.v1.backup_$TS && \
mv ~/agent_evaluator.py ~/tading-bot/ && \
mv ~/agent_rotator.py   ~/tading-bot/ && \
mv ~/ensemble.py        ~/tading-bot/ && \
mv ~/meta_agent.py      ~/tading-bot/ && \
echo "=== New files in place ===" && \
ls -la ~/tading-bot/agent_evaluator.py ~/tading-bot/agent_rotator.py ~/tading-bot/ensemble.py ~/tading-bot/meta_agent.py && \
echo "" && \
echo "=== Backups created ===" && \
ls -la ~/tading-bot/*.backup_$TS && \
echo "" && \
echo "=== Version banners (confirm new versions loaded) ===" && \
head -3 ~/tading-bot/agent_evaluator.py && echo "---" && \
head -3 ~/tading-bot/agent_rotator.py && echo "---" && \
head -3 ~/tading-bot/ensemble.py && echo "---" && \
head -3 ~/tading-bot/meta_agent.py
```

You should see all four new files, four matching `.backup_<timestamp>` siblings, and version banners showing **v2.0** (agent_evaluator) and **v1.1** (the other three). If any banner still shows the old version, the `mv` failed for that file — re-upload and re-run the `mv` line for that one file before continuing.

---

## Step 2 — Sanity check (dry runs only — no state changes)

This is the safety check. It evaluates against your real 1,423-trade ledger and tells you what it WOULD do, without actually benching anything:

```bash
echo "=== Evaluator output ===" && \
cd ~/tading-bot && python3 agent_evaluator.py && \
echo "" && \
echo "=== Rotator (DRY RUN) ===" && \
python3 agent_rotator.py --dry-run
```

What you should see:
- Full agent leaderboard with 5d / 20d / All-Time P&L
- Top performer (likely MomentumAgent)
- Flagged agents (likely **OptionsFlowAgent + TechnicalAgent + maybe SectorRotationAgent**)
- Rotator output showing `BENCHED <name>` actions for each flagged agent

If the flagged set matches what you'd expect (the bleeders you saw in the leaderboard), we're good. If something looks wrong (e.g. MomentumAgent gets flagged), STOP and show me the output — better to debug now than after live rotation runs.

---

## Step 3 — Verify ensemble.py + meta_agent.py import cleanly

Don't run a full ensemble cycle yet — just confirm both patched modules load AND that MetaAgent's new weights are non-uniform (the smoking-gun test that the soft-layer fix is working):

```bash
cd ~/tading-bot && python3 -c "
import ensemble
print('ensemble.py imports OK')
print('Bench helper exists:', hasattr(ensemble, '_load_benched_agent_names'))
print('Empty bench set when no rotation yet:', ensemble._load_benched_agent_names())
print('---')
from meta_agent import MetaAgent
m = MetaAgent()
print('meta_agent.py imports OK')
print('Profit weights (top 5 by weight):')
for name, w in sorted(m.weights.items(), key=lambda x: -x[1])[:5]:
    print(f'  {name:25} {w:.3f}')
print('Profit weights (bottom 5 by weight):')
for name, w in sorted(m.weights.items(), key=lambda x: x[1])[:5]:
    print(f'  {name:25} {w:.3f}')
all_one = all(abs(w - 1.0) < 0.001 for w in m.weights.values())
print(f'All-uniform-1.0 (broken state)? {all_one}')
"
```

Expected output:
```
ensemble.py imports OK
Bench helper exists: True
Empty bench set when no rotation yet: set()
---
meta_agent.py imports OK
Profit weights (top 5 by weight):
  MomentumAgent             1.000
  NewsAgent                 ~0.5-0.8
  SentimentAgent            ~0.2-0.4
  ...
Profit weights (bottom 5 by weight):
  OptionsFlowAgent          0.150
  TechnicalAgent            0.150
  ...
All-uniform-1.0 (broken state)? False
```

The KEY check is the very last line: **`All-uniform-1.0 (broken state)? False`**. If that says `True`, the new MetaAgent fell back to DEFAULT_WEIGHTS — the trade_ledger import or per-agent computation failed silently. STOP and show me the output. If you get any ImportError or syntax error, STOP and show me — DO NOT proceed to Step 4.

---

## Step 4 — REAL rotation (this writes bench state)

Once Steps 2+3 are clean:

```bash
cd ~/tading-bot && python3 agent_rotator.py
```

This time it'll write `active: false` for the flagged agents into `~/tading-bot/logs/agent_summary.json` and append a record to `~/tading-bot/logs/rotation_log.jsonl`.

Verify:

```bash
echo "=== Updated agent_summary.json (look for active:false entries) ===" && \
cat ~/tading-bot/logs/agent_summary.json | python3 -m json.tool && \
echo "" && \
echo "=== Rotation log ===" && \
cat ~/tading-bot/logs/rotation_log.jsonl && \
echo "" && \
echo "=== ensemble.py now reports these as benched ===" && \
python3 -c "import ensemble; print(ensemble._load_benched_agent_names())"
```

You should see the flagged agents (TechnicalAgent, OptionsFlowAgent, etc.) with `"active": false` in the JSON, and the same names in the bench set the ensemble now sees.

---

## Step 5 — Confirm benching takes effect on next ensemble tick

The next time the scheduler fires `run_ensemble()`, the log will show benched agents being skipped. To verify without waiting, watch the log:

```bash
tail -F ~/tading-bot/logs/scheduler.log | grep --line-buffered -E "Benched agents skipped|Ensemble cycle|PAPER TRADE"
```

Wait for the next ensemble cycle (within 5–10 minutes during market hours). You should see lines like:

```
[INFO] ── Ensemble cycle start 14:35:00 UTC ──
[INFO] ⏸️  Benched agents skipped this cycle: TechnicalAgent, OptionsFlowAgent
```

If you see the `⏸️` line with the right agent names, **rotation is fully wired end-to-end.** Press Ctrl+C to exit the tail.

If you don't see the `⏸️` line at all (or you see TechnicalAgent still firing PAPER TRADE lines), STOP and show me. Likely cause: the scheduler is using an old cached import — restarting the bot service should fix it.

---

## Step 6 — Add to cron (only after Step 5 confirms the loop works)

Edit crontab:

```bash
crontab -e
```

Add this line **above** the existing 4:35 PM email line, so rotation runs FIRST:

```
25 16 * * 1-5  cd /home/mddnnbr/tading-bot && /usr/bin/python3 agent_rotator.py >> /home/mddnnbr/tading-bot/logs/rotator_cron.log 2>&1
```

This runs the rotator at **4:25 PM ET** Monday-Friday (10 minutes before the email), so by the time the email is sent, any bench/promote actions are reflected in the report.

Verify:

```bash
crontab -l
```

You should see both lines now:
```
25 16 * * 1-5  cd /home/mddnnbr/tading-bot && /usr/bin/python3 agent_rotator.py ...
35 16 * * 1-5  /usr/bin/python3 /home/mddnnbr/tading-bot/daily_reporter.py --send-now
```

---

## Rollback (any combination of files)

All four originals were backed up in Step 1 with the same timestamp suffix. Restore any or all:

```bash
cd ~/tading-bot && \
TS_LATEST=$(ls -t agent_evaluator.py.v1.backup_* | head -1 | sed 's/.*backup_//') && \
cp agent_evaluator.py.v1.backup_$TS_LATEST agent_evaluator.py && \
cp agent_rotator.py.v1.backup_$TS_LATEST   agent_rotator.py && \
cp ensemble.py.v1.backup_$TS_LATEST        ensemble.py && \
cp meta_agent.py.v1.backup_$TS_LATEST      meta_agent.py && \
echo "Rolled back all four files to pre-deploy state"
```

To remove the cron line:

```bash
crontab -e
# delete the line starting with: 25 16 * * 1-5
```

To re-activate every benched agent immediately (clear the bench, no code change needed):

```bash
python3 -c "
import json
from pathlib import Path
p = Path.home() / 'tading-bot' / 'logs' / 'agent_summary.json'
data = json.load(open(p))
for name in data:
    if isinstance(data[name], dict):
        data[name]['active'] = True
        data[name]['benched_at'] = None
json.dump(data, open(p,'w'), indent=2)
print('All agents re-activated')
"
```

The patched ensemble will pick this up on its very next cycle — no restart needed.

---

## What this does NOT change (intentionally)

- **Bench duration** — still 3 days (`BENCH_DAYS = 3` in agent_rotator.py). After 3 days, benched agents auto-reactivate and get a fresh chance.
- **Variant substitutions** — still uses the existing `AGENT_VARIANTS` map.
- **Underperform threshold** — still 20% (`UNDERPERFORM_THRESHOLD = 0.20`). If you want stricter or more lenient flagging, change that knob in `agent_evaluator.py`.
- **MetaAgent profit weighting source** — *now fixed in this deploy.* `meta_agent.py._load_performance_weights()` reads from `trade_ledger` and applies the existing power curve (`PROFIT_WEIGHT_EXPONENT = 0.6`), `MIN_AGENT_WEIGHT = 0.15` floor, and 3-win-streak bonus. The KNOBS themselves (exponent, floor, streak threshold) are unchanged — same shape of curve, just fed from real data now. Tune those knobs later if rotation+weighting still leaves too many losing trades through.
- **Daily reporter** — completely independent. The reporter reads from the same ledger but doesn't care about bench/active state. (Phase 3 idea: add a "Bench status" panel to the email so you can see at a glance which agents are sitting out.)
