# BluSterling trading bot — architecture review and Sep 10, 2026 fixes

Paper trading only. Live trading is not wired and this PR hard-refuses it.

## What this system is

A 16-agent ensemble (docs still say 12) plus MetaAgent, running on a GCP
e2-micro as systemd `trading-bot`. `market_scheduler.py` ticks every 60s
during NYSE hours. Auto-deploy pulls **upstream** `mddnnbr-tech/trading-bot`
master every 60s (`trading-bot-deploy`). This repo is a fork
(`michaeldnbaker-ops/trading-bot`).

Money path:

```
agents → MetaAgent.synthesize → AgentRiskBridge → options_executor
                                              ↘ order_executor (shares + GTC trail)
                                                    ↓
                                              Alpaca paper API   ← TRUTH for money
                                                    ↓
                                              trade_ledger.csv   ← TRUTH for attribution
                                                    ↓
                                              report_data.snapshot → daily_reporter email
```

Risk path (before this PR): halt / daily cap / buying power / net-long /
gross / shorts-in-bear / learner avoid-list / ATR geometry / falling-knife
/ one-position-per-symbol. After this PR: same, plus **exits-required**
kill-switch (no new entries while any equity position is naked).

## Sep 9, 2026 email — what was actually true

Reported: CRITICAL 7 naked positions (AMD, ANF, BAC, CRWD, DLTR, META, PSQL),
ghost SUNB, 174 errors / 0 signals, equity ≈ −16.8% vs SPY +5.4%.

| Symptom | Root cause | Was the Aug fix enough? |
|---|---|---|
| 7 positions, no exit orders | `invariants.py` **flags** naked positions. Nothing on the 60s tick **re-armed** them. Aug 14 (`3d42456`) fixed *new* partial-fill stop sizing. Aug 13 (`73012c8`) stopped trail-ratchet churn. The cancel-then-submit window in `widen_trails_on_survivors`, late fills after the 15s wait, and any rejected trail still left the book naked until a human noticed the email. | Incomplete — detection without a healer. |
| Ghost SUNB | `sync_from_broker()` only **re-opens** orphans. Ghosts (ledger open, broker empty) waited for `MAX_HOLD_DAYS` (5) or a simulated stop/target hit. A name the broker never held sat "open" and inflated exposure/gates. | Not addressed. |
| 174 errors / 0 signals | Two reporting bugs, not necessarily a dead bot. (1) `daily_reporter` still parsed the **v11** log line `Total raw signals from all agents:` — current ensemble logs `Total raw signals: N`. (2) KPI `approved_count` was `len(trade_log.jsonl)` which nothing writes. Agent failures logged `ERROR`+traceback every tick. | Reporter regression. Error spam unthrottled. |
| −16.8% vs SPY | Broker equity is already the report money source (`report_data.snapshot`). The drawdown is real paper P&L, not a ledger fiction. This PR does not claim to make the book beat SPY; it stops unbounded downside and lying emails. | N/A |

Aug commits verified still present on master (not reverted):

- `3d42456` Size stop from actual fill, wait for terminal `filled` not `*filled`
- `58122d6` Size from live broker equity, not stale `ACCOUNT_BALANCE`
- `26f2305` / `28801b0` Exposure gates recalibrated after the Aug 14 deadlock
- `73012c8` Trail ratchet only tightens
- `a876d53` Protection check matches order side to position side

They were **not deployed as a closed loop**. The invariant email was the
backstop. That is why Sep 9 still looked like Aug 4.

## Fixes in this PR

1. **Tick-level exit backstop** — `order_executor.ensure_protective_exits()`
   runs at the start of every scheduler tick and every ensemble cycle,
   *before* daily-cap / buying-power / halt returns. Adds a GTC trailing
   stop sized to the broker's actual qty. Never cancels an existing
   protective order. New entries are blocked while any equity is still naked.
2. **Trail submit retries** — if the post-fill trail rejects, retry 3× then
   still record the ledger row and let the backstop finish the job.
   `widen_trails_on_survivors` re-runs the backstop after cancel-then-submit.
3. **Ghost close** — `trade_ledger.close_ghosts()` closes ledger rows the
   broker does not hold (fill when available; skip last 120s / open orders /
   crypto / options). Also closes inside `refresh_open_positions` instead of
   waiting five days.
4. **Paper lock** — `TradingClient(..., paper=True)` always. `PAPER_TRADING=false`
   and `TRADING_MODE=live` are refused. Live trading cannot be enabled by env.
5. **Daily email** — blue PAPER banner, `[PAPER]` subject, broker snapshot
   still owns money, findings restored at the top, system errors vs fetch/404s
   split, signal counts parsed from current log lines and the ledger.
6. **Health check** — hourly cron now *heals* (re-arm exits, close ghosts)
   instead of only paging.

## Deploy

VM auto-deploys from **upstream** `mddnnbr-tech/trading-bot` master, not
this fork. If this PR landed only on `michaeldnbaker-ops/trading-bot`,
Michael must merge it upstream or the VM will never see it.

No `.env` changes required. Do not set `PAPER_TRADING=false`. Do not set
`TRADING_MODE=live`. After merge: `python3 config_check.py` on the VM
(or trust CI-equivalent `python3 test_risk_health.py`). systemd restart
happens automatically on the new commit.

## What this PR does not do

- Does not enable live trading.
- Does not retune agents to beat SPY. That is a research problem; the
  book is at net 100% / pos 10% / cap 3 / shorts in bear only (Aug 15
  measured config). Unbounded downside was the emergency.
- Does not collapse the five closers / seven P&L computers into one
  module. Invariants + healers make divergence loud and self-correcting.
- Does not add secrets to git.
