"""
ensemble.py  (replaces ensemble_v11.py — market_scheduler imports THIS file)
─────────────────────────────────────────────────────────────────────────────
Orchestrates the full 12-agent pipeline each trading tick.

Changes vs v11:
  • Alpaca real-time stream is started at first import; agents query the
    shared price cache via alpaca_stream.get_latest_price() when available,
    falling back to yfinance automatically.
  • Surge detection: after each tick, check for real-time surges/drops and
    emit opportunistic signals (fast-moving stocks caught by Alpaca streaming).
  • Strategy learner hooks: after cycle, record which agents fired and any
    outcome data available so the learner can improve thresholds over time.

Pipeline (one full cycle):
  1. RegimeDetector.detect()            → identify current market regime
  2. RiskAgent.assess()                 → get risk status / halt check
  3. All 12 agents generate signals     → raw signal list
  4. Surge scan (Alpaca)                → catch real-time breakouts/drops
  5. MetaAgent.synthesize()             → merge + weight by P&L + regime
  6. AgentRiskBridge.evaluate_signal()  → 7-gate validation
  7. PerformanceLogger                  → log all results
  8. Return approved signals for order execution
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("Ensemble")

PAPER_TRADING   = os.getenv("PAPER_TRADING", "true").lower() == "true"
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "16000"))

# Hard cap on NEW equity entries per trading day. The concentrated-risk
# mandate (2026-07-15): 3-4 big high-conviction trades a day, not a stream
# of small ones — Jul 13 alone opened 15 positions under per-tick caps,
# which is exactly the bleed pattern the clean-epoch data convicted.
DAILY_TRADE_CAP = int(os.getenv("DAILY_TRADE_CAP", "4"))

# Runaway circuit breaker only — entries are gated at 3x this, NOT at this
# value (changed 2026-08-07). History: 8 entries/day stacked 25 open
# positions by 2026-07-23 and buying power hit ~$0, so a count cap was
# added. But count was always a proxy for the real constraint, capital,
# and the proxy failed loudly on 2026-08-07: a ledger reconciliation left
# 254 phantom "open" rows against a cap of 15 and froze ALL trading during
# the first week the system beat SPY. Buying power (the $20k reserve gate)
# and leverage (2.5x ceiling) are the true limits, both enforced below and
# asserted every 20 min by invariants.py.
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "10"))

# Hard cap on the learner's blacklist. Guards against the failure found
# 2026-07-30: a learner trained on corrupted-era data blacklisted 40
# symbols (the entire universe) and would have stopped the bot trading.
MAX_AVOID_SYMBOLS = int(os.getenv("MAX_AVOID_SYMBOLS", "8"))

AGENT_SUMMARY_PATH = Path(__file__).resolve().parent / "logs" / "agent_summary.json"

# ── Start Alpaca streaming at import time ─────────────────────────────────────
try:
    import alpaca_stream
    alpaca_stream.start()
    _ALPACA_OK = True
except Exception as _e:
    log.warning(f"Alpaca streaming unavailable: {_e} — using yfinance only")
    _ALPACA_OK = False


def _load_benched_agent_names() -> set[str]:
    try:
        if not AGENT_SUMMARY_PATH.exists():
            return set()
        with open(AGENT_SUMMARY_PATH) as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return set()
        return {
            name for name, info in data.items()
            if isinstance(info, dict) and info.get("active", True) is False
        }
    except Exception as e:
        log.warning(f"Could not read agent_summary.json ({e}) — treating all as active")
        return set()


class Ensemble:
    """Full 12-agent ensemble. One instance per scheduler tick."""

    def __init__(self):
        from technical_agent       import TechnicalAgent
        from news_agent            import NewsAgent
        from sentiment_agent       import SentimentAgent
        from momentum_agent        import MomentumAgent
        from breakout_agent        import BreakoutAgent
        from bearish_pattern_agent import BearishPatternAgent
        from short_momentum_agent  import ShortMomentumAgent
        from earnings_agent        import EarningsAgent
        from macro_agent           import MacroAgent
        from premarket_agent       import PremarketAgent
        from sector_rotation_agent import SectorRotationAgent
        from options_flow_agent    import OptionsFlowAgent
        from volatility_agent      import VolatilityAgent
        from intermarket_agent     import IntermarketAgent
        from movers_agent          import MoversAgent
        from risk_agent            import RiskAgent
        from meta_agent            import MetaAgent
        from agent_risk_bridge     import AgentRiskBridge
        from performance_logger    import PerformanceLogger
        from regime_detector       import RegimeDetector

        self.agents = [
            TechnicalAgent(),
            NewsAgent(),
            SentimentAgent(),
            MomentumAgent(),
            BreakoutAgent(),
            BearishPatternAgent(),
            ShortMomentumAgent(),
            EarningsAgent(),
            MacroAgent(),
            PremarketAgent(),
            SectorRotationAgent(),
            OptionsFlowAgent(),
            VolatilityAgent(),
            IntermarketAgent(),
            MoversAgent(),
        ]

        self.risk   = RiskAgent()
        self.meta   = MetaAgent()
        self.bridge = AgentRiskBridge(account_balance=ACCOUNT_BALANCE)
        self.logger = PerformanceLogger()
        self.regime = RegimeDetector()

        mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
        alpaca_status = "Alpaca streaming LIVE" if (_ALPACA_OK and alpaca_stream.is_streaming()) else "yfinance only"
        log.info(f"Ensemble initialized — {mode} — {len(self.agents)} agents — {alpaca_status}")

    def run_cycle(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        log.info(f"── Ensemble cycle start {now.strftime('%H:%M:%S UTC')} ──")

        # Step 1: regime
        try:
            regimes = self.regime.detect()
        except Exception as e:
            log.warning(f"RegimeDetector failed: {e} — NEUTRAL")
            regimes = {"NEUTRAL"}

        # Step 1b: manage open option exits every tick — options have no
        # broker-side trailing stop, so profit/stop/theta rules run here.
        try:
            from options_executor import manage_options_exits
            manage_options_exits()
        except Exception as _me:
            log.debug(f"options exit manager: {_me}")

        # Step 1c: widen trails on positions that survived 2+ days —
        # the 5d+ bucket is the only profitable holding period we have.
        try:
            from order_executor import widen_trails_on_survivors
            widen_trails_on_survivors()
        except Exception as _we:
            log.debug(f"trail widening: {_we}")

        # Step 2: risk gate
        risk_status = self.risk.assess()
        if risk_status["halt_trading"]:
            log.warning(f"TRADING HALTED: {risk_status['warnings']}")
            # A halt that only blocks new entries is not risk management —
            # on 2026-07-29 it would have frozen the book fully long while
            # the market fell another 1%. Cut the losers on the way out.
            self._derisk_on_halt()
            return []

        # Step 2a: daily entry budget — once DAILY_TRADE_CAP equity positions
        # have opened today, we're done adding risk until tomorrow.
        try:
            import trade_ledger as _tl
            from datetime import datetime as _dt
            _today = _dt.now(_tl.ET).strftime("%Y-%m-%d")
            opened_today = len([t for t in _tl.trades_on_date(_today)
                                if "/" not in t.symbol])
        except Exception:
            opened_today = 0
        try:
            from auto_tune import load as _at_cfg
            _cap = int(_at_cfg().get("daily_trade_cap", DAILY_TRADE_CAP))
        except Exception:
            _cap = DAILY_TRADE_CAP
        entries_remaining = _cap - opened_today
        # Concentrate entries in the first hour. Evidence 2026-07-31:
        # 9-10am ran +$102/trade against the rest of the session on 38
        # vs 95 trades. After 10am, hold back half the daily budget so
        # the good window is never starved by mediocre later setups.
        try:
            from datetime import datetime as _d2
            import trade_ledger as _tl2
            if _d2.now(_tl2.ET).hour >= 10:
                entries_remaining = min(entries_remaining, max(1, _cap // 2))
        except Exception:
            pass
        if entries_remaining <= 0:
            log.info(f"Daily trade cap reached ({opened_today}/{DAILY_TRADE_CAP}) "
                     f"— managing open positions only, no new entries today")
            return []
        try:
            open_count = len([t for t in _tl.open_positions() if "/" not in t.symbol])
        except Exception:
            open_count = 0
        # Position COUNT no longer gates entries (2026-08-07). The count was
        # always a crude proxy for the real constraint, which is capital —
        # and it misfired badly: a ledger reconciliation left 254 phantom
        # "open" rows against a cap of 15 and froze all trading, on a week
        # the system was finally beating SPY. Capital and leverage are the
        # true limits and both are enforced below and asserted every 20
        # minutes by invariants.py (buying-power floor, 2.5x leverage
        # ceiling). MAX_OPEN_POSITIONS is retained only as a far-outlier
        # circuit breaker for runaway accumulation.
        if open_count >= MAX_OPEN_POSITIONS * 3:
            log.warning(f"Runaway position count ({open_count}) — halting entries; "
                        f"this indicates a reconciliation fault, not normal trading")
            return []

        # Capital gate — the real constraint. Deploy freely while buying
        # power exists; stand down just before the wall instead of firing
        # doomed orders into $0 bp all day (2026-07-23: 597 failed
        # submissions). Reserve = ~2 positions of headroom.
        try:
            from order_executor import get_executor
            _client = get_executor()._client
            if _client is not None:
                bp = float(_client.get_account().buying_power)
                if bp < 20_000:
                    log.info(f"Buying power ${bp:,.0f} below reserve — "
                             f"managing open positions only this tick")
                    return []
        except Exception:
            pass

        # Step 2b: dynamic universe injection — the whole market via funnel.
        # Static watchlists cover ~40 core names; the market-wide screens
        # (gainers/losers/most-active) find whatever ELSE is moving today —
        # any listed stock, IPOs included — and inject it into the scanning
        # agents' watchlists for this tick. This is how "look at all stocks"
        # actually works at 60s ticks: cheap screens select the active
        # subset, the full agent stack analyzes it, and multi-agent
        # consensus (incl. corroborated shorts) becomes possible on names
        # no static list ever contained.
        dynamic = self._dynamic_universe()
        dynamic = list(dict.fromkeys(list(dynamic) + list(Ensemble._cross_pollinate)))
        if dynamic:
            for agent in self.agents:
                wl = getattr(agent, "watchlist", None)
                if isinstance(wl, list):
                    if not hasattr(agent, "_base_watchlist"):
                        agent._base_watchlist = list(wl)
                    agent.watchlist = agent._base_watchlist + [
                        s for s in dynamic if s not in agent._base_watchlist]

        # Step 3: gather signals from active agents
        benched = _load_benched_agent_names()
        skipped: list[str] = []
        all_raw_signals: list[dict] = []

        for agent in self.agents:
            if agent.name in benched:
                skipped.append(agent.name)
                continue
            try:
                signals = agent.generate_signals()
                if signals:
                    log.info(f"{agent.name}: {len(signals)} signal(s)")
                all_raw_signals.extend(signals)
            except Exception as e:
                log.error(f"{agent.name} failed: {e}", exc_info=True)

        if skipped:
            log.info(f"⏸  Benched agents skipped: {', '.join(skipped)}")

        # CROSS-POLLINATION — the corroboration fix.
        # Measured 2026-07-31: 23 of 68 blocked signals were on symbols NO
        # other agent had in its watchlist (AXTI, VCYT, RDDT, AMBA...).
        # NewsAgent finds them via RSS and MoversAgent via screens, but the
        # technical agents never LOOKED at them — so "needs 2 agents" was
        # unsatisfiable by construction, not by disagreement. Any symbol
        # signalled this tick is added to every agent's watchlist so it
        # gets a real second opinion next tick.
        try:
            signalled = {s.get("symbol") for s in all_raw_signals
                         if s.get("symbol") and "/" not in s.get("symbol", "")}
            if signalled:
                Ensemble._cross_pollinate |= signalled
                Ensemble._cross_pollinate = set(list(Ensemble._cross_pollinate)[-40:])
        except Exception:
            pass

        # Step 4: Alpaca surge scan — catch real-time moves
        surge_signals = self._scan_surges(risk_status)
        if surge_signals:
            log.info(f"AlpacaSurge: {len(surge_signals)} real-time signal(s)")
            all_raw_signals.extend(surge_signals)

        log.info(f"Total raw signals: {len(all_raw_signals)}")

        if not all_raw_signals:
            log.info("No raw signals this tick — conditions not met.")
            return []

        # Step 5: MetaAgent synthesis
        try:
            synthesized = self.meta.synthesize(all_raw_signals, risk_status, regimes)
        except Exception as e:
            log.error(f"MetaAgent synthesis failed: {e}", exc_info=True)
            return []

        if not synthesized:
            log.info("MetaAgent: no signals passed synthesis threshold.")
            return []

        # Step 6: risk bridge validation
        approved = []
        for signal in synthesized:
            try:
                # Normalize stop/target to the symbol's real volatility.
                # The fixed 2%-stop/5%-target geometry killed the clean
                # epoch: 39 of 49 trades stopped out on ordinary intraday
                # noise (20% win rate vs the 29% that geometry requires).
                # ATR-derived levels give volatile names room to breathe
                # and quiet names tighter, reachable targets.
                # Apply what the weekly learner actually learned. Until
                # 2026-07-30 strategy_learner wrote learned_params.json and
                # NOTHING read it — it had flagged XLE/QQQ/META/AMD/XOM as
                # persistent losers weeks ago while the bot kept trading
                # them (META -$877, XLE -$31 in one prune). A learning loop
                # whose output is never consumed is a diary, not learning.
                if signal["symbol"] in self._avoid_symbols():
                    log.info(f"🧠 SKIPPED: {signal['symbol']:6} {signal['direction']:5} "
                             f"— on learner's avoid list (persistent loser)")
                    continue

                signal = self._normalize_geometry(signal)
                if signal.get("_falling_knife"):
                    log.info(f"🔪 SKIPPED: {signal['symbol']:6} long — falling knife "
                             f"({signal['_falling_knife']}); gap risk exceeds trail protection")
                    continue
                # Dedup gate: one open position per symbol, either side.
                # Same-side re-entry caused the duplicate-position pileup;
                # opposite-side entry fails anyway at Alpaca ("bracket orders
                # must be entry orders" — a new bracket can't open against an
                # existing position it would partially close). Block both.
                import trade_ledger as _ledger
                if (_ledger.has_open_position(signal["symbol"], "LONG")
                        or _ledger.has_open_position(signal["symbol"], "SHORT")):
                    log.info(
                        f"⏭  SKIPPED: {signal['symbol']:6} {signal['direction']:5} "
                        f"— already have an open position on this symbol"
                    )
                    continue

                if len(approved) >= entries_remaining:
                    log.info(f"⏭  Daily entry budget exhausted this tick "
                             f"({DAILY_TRADE_CAP}/day) — skipping remaining signals")
                    break

                result = self.bridge.evaluate_signal(signal)
                if result["approved"]:
                    log.info(
                        f"✅ APPROVED: {signal['symbol']:6} {signal['direction']:5} "
                        f"conf={signal['confidence']:.2f} tier={result.get('account_tier', '?')}"
                    )
                    approved.append(result)
                    # High-conviction signals route to OPTIONS (defined
                    # risk: max loss = premium). Everything else, and any
                    # option that can't find a liquid contract, falls back
                    # to shares.
                    opt = None
                    try:
                        from trade_context import record_entry
                        record_entry(result, regime=",".join(sorted(regimes)),
                                     vix=float(risk_status.get("vix") or 0))
                    except Exception:
                        pass
                    try:
                        from options_executor import execute_options_trade
                        opt = execute_options_trade(result)
                    except Exception as _oe:
                        log.warning(f"options route failed: {_oe}")
                    if opt is None:
                        from order_executor import execute_signal
                        execute_signal(result)
                else:
                    log.info(
                        f"⛔ REJECTED: {signal['symbol']:6} — "
                        f"{result.get('rejection_reason', 'unknown')}"
                    )
                    self.logger.log_signal_rejected(
                        agent_name=signal.get("agent", "Unknown"),
                        symbol=signal["symbol"],
                        reason=result.get("rejection_reason", "bridge_rejection"),
                    )
            except Exception as e:
                log.error(f"Bridge eval failed for {signal.get('symbol')}: {e}", exc_info=True)

        log.info(
            f"── Cycle: {len(all_raw_signals)} raw → "
            f"{len(synthesized)} synthesized → {len(approved)} approved ──"
        )
        return approved

    def _scan_surges(self, risk_status: dict) -> list[dict]:
        """Use Alpaca real-time data to catch surges/drops ≥ 3%."""
        if not (_ALPACA_OK and alpaca_stream.is_streaming()):
            return []
        if risk_status.get("halt_trading"):
            return []

        surges = alpaca_stream.detect_surges(threshold_pct=1.5)
        signals = []
        for s in surges[:5]:  # cap at 5 surge signals per cycle
            symbol    = s["symbol"]
            pct       = s["pct_move"]
            direction = "long" if s["direction"] == "up" else "short"
            price     = s["price"]

            if direction == "long":
                stop   = round(price * 0.975, 2)
                target = round(price * 1.05,  2)
                strat  = "single_leg_calls"
            else:
                stop   = round(price * 1.025, 2)
                target = round(price * 0.95,  2)
                strat  = "single_leg_puts"

            # Lower confidence for surge signals — they need corroboration
            confidence = min(0.65 + abs(pct) * 0.02, 0.78)

            signals.append({
                "agent":           "AlpacaSurgeDetector",
                "strategy":        strat,
                "instrument_type": "options",
                "symbol":          symbol,
                "direction":       direction,
                "entry_price":     round(price, 2),
                "stop_loss_price": stop,
                "target_price":    target,
                "option_premium":  None,
                "futures_symbol":  None,
                "confidence":      round(confidence, 3),
                "expiration":      _today_expiry(),
                "meta_score":      round(confidence, 3),
                "regime_affinity": [],
                "reasons":         [f"Real-time surge {pct:+.1f}% on {s['volume']:,.0f} shares"],
                "timestamp":       datetime.now(timezone.utc).isoformat(),
            })
        return signals

    _avoid_cache: tuple[float, set] = (0.0, set())

    @classmethod
    def _avoid_symbols(cls) -> set:
        """Symbols the weekly learner flagged as persistent losers.

        Refreshed every 10 minutes so a Friday learning run takes effect
        without a restart. Capped at 40 so one bad week can't blacklist
        the entire tradeable universe.
        """
        import time, json
        ts, cached = cls._avoid_cache
        if time.time() - ts < 600:
            return cached
        # Rank by REALIZED DAMAGE in the clean epoch, not by the learner's
        # raw avoid-list. That list was computed over 4,213 pre-epoch
        # trades from the corrupted era and named 40 symbols — AAPL, MSFT,
        # NVDA, GOOGL, QQQ, essentially the whole universe — because during
        # that period everything lost to bugs, not to the symbols. Trusting
        # it verbatim would have halted trading. Blacklist only the worst
        # few, and only on evidence from trustworthy data.
        out: set = set()
        try:
            import trade_ledger as _tl
            from collections import defaultdict
            dmg = defaultdict(lambda: [0.0, 0, 0])   # pnl, trades, wins
            for t in _tl.epoch_trades():
                if t.is_open:
                    continue
                p = t.realized_pnl or 0.0
                d = dmg[t.symbol]
                d[0] += p; d[1] += 1; d[2] += (p > 0)
            ranked = sorted(
                (s for s, d in dmg.items() if d[1] >= 4 and d[0] < 0
                 and (d[2] / d[1]) < 0.35),
                key=lambda s: dmg[s][0])
            out = set(ranked[:MAX_AVOID_SYMBOLS])
        except Exception as e:
            log.debug(f"avoid-list computation failed: {e}")
        cls._avoid_cache = (time.time(), out)
        if out:
            log.info(f"🧠 Learner avoid-list active ({len(out)}): {', '.join(sorted(out))}")
        return out

    _derisked_on: str = ""      # ET date the halt de-risk already ran

    def _derisk_on_halt(self) -> None:
        """Liquidate losing positions when the daily loss limit trips.

        Winners keep their trailing stops — they're the asymmetry engine
        and may be the hedge that's actually working. Losers are cut so a
        bad day can't compound into a catastrophic one. Runs once per day.
        """
        from datetime import datetime as _dt
        try:
            import trade_ledger as _tl
            today = _dt.now(_tl.ET).strftime("%Y-%m-%d")
        except Exception:
            today = "unknown"
        if Ensemble._derisked_on == today:
            return
        Ensemble._derisked_on = today

        try:
            from order_executor import get_executor
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            import time as _t
            client = get_executor()._client
            if client is None:
                return
            cut, kept = [], []
            for p in client.get_all_positions():
                sym, pl = str(p.symbol), float(p.unrealized_pl)
                if sym.endswith("USD") and len(sym) > 5:
                    continue                      # crypto: own scheduler
                if pl >= 0:
                    kept.append(sym)
                    continue
                try:
                    for o in client.get_orders(GetOrdersRequest(
                            status=QueryOrderStatus.OPEN, symbols=[sym])):
                        client.cancel_order_by_id(o.id)
                    _t.sleep(0.4)
                    client.close_position(sym)
                    cut.append(f"{sym}({pl:+.0f})")
                except Exception as e:
                    log.warning(f"de-risk: could not close {sym}: {e}")
            log.warning(f"🛑 DE-RISK on halt — cut losers: {', '.join(cut) or 'none'} "
                        f"| winners left riding: {', '.join(kept) or 'none'}")
        except Exception as e:
            log.error(f"de-risk failed: {e}", exc_info=True)

    # Symbols any agent signalled recently — injected into every agent's
    # watchlist so corroboration is possible rather than structurally denied.
    _cross_pollinate: set = set()

    _universe_cache: tuple[float, list[str]] = (0.0, [])

    def _dynamic_universe(self, max_symbols: int = 15) -> list[str]:
        """Today's hottest listed stocks from market-wide screens.

        Cached 5 minutes — the set of names worth deep analysis doesn't
        change tick-to-tick, and each injected symbol costs the scanning
        agents a data fetch. Filters: $5+ price, 500k+ volume, plain US
        listings only.
        """
        import time
        ts, cached = Ensemble._universe_cache
        if time.time() - ts < 300:
            return cached
        symbols: list[str] = []
        try:
            import yfinance as yf
            for screen in ("day_gainers", "day_losers", "most_actives"):
                try:
                    for q in (yf.screen(screen).get("quotes") or [])[:10]:
                        sym   = q.get("symbol", "")
                        price = float(q.get("regularMarketPrice") or 0)
                        vol   = float(q.get("regularMarketVolume") or 0)
                        if (sym and "." not in sym and "-" not in sym
                                and price >= 5 and vol >= 500_000
                                and sym not in symbols):
                            symbols.append(sym)
                except Exception:
                    continue
        except Exception as e:
            log.debug(f"dynamic universe unavailable: {e}")
        symbols = symbols[:max_symbols]
        Ensemble._universe_cache = (time.time(), symbols)
        if symbols:
            log.info(f"🌐 Dynamic universe this tick: {', '.join(symbols)}")
        return symbols

    @staticmethod
    def _normalize_geometry(signal: dict) -> dict:
        """Re-derive stop/target from daily ATR(14): stop 1.5x, target 2.5x.

        Daily bars, not 5-minute — positions are held for days under GTC
        brackets, so the stop must survive a normal day's range. Breakeven
        win rate at this 1.5:2.5 geometry is 37.5%. Floor of 1% of entry
        guards against ultra-quiet symbols producing paper-thin stops.
        Falls back to the agent's own levels if data is unavailable.
        """
        symbol = signal.get("symbol", "")
        if "/" in symbol:            # crypto — its own scheduler handles it
            return signal
        try:
            import pandas as pd
            import yfinance as yf
            df = yf.Ticker(symbol).history(period="3mo", interval="1d")
            if df is None or len(df) < 20:
                return signal
            prev_close = df["Close"].shift(1)
            tr = pd.concat([
                df["High"] - df["Low"],
                (df["High"] - prev_close).abs(),
                (df["Low"] - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = float(tr.rolling(14).mean().iloc[-1])
            entry = float(signal.get("entry_price") or 0)
            if atr <= 0 or entry <= 0:
                return signal

            # Falling-knife guard: never buy a LONG into a multi-day
            # collapse. Trailing stops cannot protect against overnight
            # gaps — AMKR was bought long 2026-07-27 mid-collapse (-8.6%
            # the prior day), gapped -14.5% overnight, and lost $1,170 on
            # a 6% trail: 4x the intended per-trade risk. A stock in
            # freefall is a SHORT candidate, not a long one.
            if signal.get("direction") == "long" and len(df) >= 3:
                two_day = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-3]) - 1) * 100
                if two_day <= -8.0:
                    signal["_falling_knife"] = f"{two_day:.1f}% over 2 days"
            # ATR multiple is auto-tuned weekly from post-mortem data
            try:
                from auto_tune import load as _tl_cfg
                _mult = float(_tl_cfg().get("atr_stop_mult", 1.5))
            except Exception:
                _mult = 1.5
            # Cap stop width at 4% of entry. Evidence 2026-07-31: the
            # 4%+ ATR bucket held 50 trades and -$3,604 — nearly all
            # losses — vs +$200 for 2-4%. A volatile name does not earn
            # a wider stop, it earns a smaller position.
            stop_dist = max(_mult * atr, entry * 0.01)
            stop_dist = min(stop_dist, entry * 0.04)
            # target_price is a distant bookkeeping marker (4x stop) — the
            # real exit is the broker-side trailing stop, which is uncapped
            # on winners. Keeping the marker far out stops the ledger's
            # price simulation from fake-closing runners at +2.5 ATR.
            if signal.get("direction") == "long":
                signal["stop_loss_price"] = round(entry - stop_dist, 2)
                signal["target_price"]    = round(entry + stop_dist * 4.0, 2)
            else:
                signal["stop_loss_price"] = round(entry + stop_dist, 2)
                signal["target_price"]    = round(entry - stop_dist * 4.0, 2)
        except Exception:
            pass
        return signal

    def _log_paper_trade(self, approved_signal: dict):
        symbol    = approved_signal.get("symbol", "")
        direction = approved_signal.get("direction", "long")
        entry     = approved_signal.get("entry_price", 0)
        target    = approved_signal.get("target_price", entry)
        stop      = approved_signal.get("stop_loss_price", entry)
        agent     = approved_signal.get("agent", "MetaAgent")
        log.info(
            f"📋 PAPER TRADE: {symbol} {direction.upper()} "
            f"entry=${entry} target=${target} stop=${stop} agent={agent}"
        )


def _today_expiry() -> str:
    """Nearest Friday from today."""
    from datetime import timedelta
    today  = datetime.now(timezone.utc).date()
    days   = (4 - today.weekday()) % 7
    if days == 0:
        days = 7
    return (today + timedelta(days=days)).strftime("%Y-%m-%d")


_ensemble_instance: "Ensemble | None" = None

def run_ensemble() -> list[dict]:
    """
    Entry point called by market_scheduler.py each tick.
    Reuses the same Ensemble instance across ticks to avoid
    re-importing all 12 agents and re-loading weights every minute.
    """
    global _ensemble_instance
    if _ensemble_instance is None:
        _ensemble_instance = Ensemble()
    return _ensemble_instance.run_cycle()


if __name__ == "__main__":
    results = run_ensemble()
    print(f"\n{'='*60}")
    print(f"Ensemble complete: {len(results)} approved signal(s)")
    for r in results:
        print(f"\n  {r['symbol']} {r['direction'].upper()}")
        print(f"  Agent:      {r.get('agent', '?')}")
        print(f"  Confidence: {r.get('confidence', '?')}")
        print(f"  Entry:      ${r.get('entry_price', '?')}")
        print(f"  Stop:       ${r.get('stop_loss_price', '?')}")
        print(f"  Target:     ${r.get('target_price', '?')}")
