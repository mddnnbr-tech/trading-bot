"""
config_check.py
───────────────
Pre-deploy smoke test. Catches the class of bug that has caused most of the
recent breakage: a change that is individually reasonable but does not
actually CONNECT to the thing it was meant to affect.

Every failure below is one that shipped to a live bot this week:

  regime name typo      MeanReversionAgent declared ["BULL","NEUTRAL",
                        "VOLATILE"]; regime_detector emits BULL_TREND /
                        HIGH_VOL. The intersection never matched, so the
                        agent was silent in the regime it is best in.
  undefined import      meta_agent used os.getenv without importing os —
                        would have crashed the module and taken the bot down.
  arithmetic mute       regime aversion subtracted 0.35 from agents already
                        on the 0.40 floor, leaving 0.15. No short could ever
                        clear the 0.72 solo bar; a lean became a gag.
  gate deadlock         gross and net entry gates set so that no entry of
                        either direction could pass.

None of these needed a backtest to catch. They needed thirty seconds of
verification that nobody was doing. Run this before every restart.

    python3 config_check.py   # exit 0 = safe to deploy
"""

from __future__ import annotations

import sys
import traceback

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg):
    FAILURES.append(msg)


def warn(msg):
    WARNINGS.append(msg)


def check_imports():
    """Every module must import cleanly — an undefined name here is fatal."""
    mods = ["ensemble", "meta_agent", "agent_risk_bridge", "order_executor",
            "trade_ledger", "invariants", "exposure", "regime_detector",
            "agent_evaluator", "agent_rotator", "report_data"]
    for m in mods:
        try:
            __import__(m)
        except Exception as e:
            fail(f"import {m}: {type(e).__name__}: {e}")


def check_regime_vocabulary():
    """Agent regime labels must be words RegimeDetector actually emits.

    A typo here fails silently and forever: set intersection just returns
    empty, so the agent quietly never gets its boost or its penalty.
    """
    import inspect
    import regime_detector
    src = inspect.getsource(regime_detector)
    import re
    vocab = set(re.findall(r'"([A-Z][A-Z_]{2,})"', src))
    if not vocab:
        warn("could not extract regime vocabulary — skipping name check")
        return
    try:
        import ensemble
        agents = ensemble.Ensemble().agents
    except Exception as e:
        fail(f"cannot build ensemble for regime check: {e}")
        return
    for a in agents:
        for attr in ("regime_affinity", "regime_aversion"):
            for name in (getattr(a, attr, None) or []):
                if name not in vocab:
                    fail(f"{a.name}.{attr} contains '{name}' which "
                         f"RegimeDetector never emits — this will silently "
                         f"never match. Valid: {sorted(vocab)}")


def check_weight_arithmetic():
    """Verify how weight actually interacts with the gates.

    The first version of this check asserted that weighted confidence must
    clear the solo bar, and reported two FAILUREs. Both were false: the
    solo gates read RAW confidence (meta_agent lines ~190, ~207, ~248,
    ~279, and ~280 restores raw for solo signals), so weight never blocks
    a solo entry. Acting on that reading would have "fixed" a bug that did
    not exist — the exact failure mode this file is meant to prevent, so
    the check is kept honest rather than deleted.

    What weight ACTUALLY controls is ranking: weighted confidence decides
    which signals survive MAX_SIGNALS_PER_TICK. That is the real
    suppression channel and the one worth asserting.
    """
    import inspect

    import meta_agent as M
    src = inspect.getsource(M)

    # Guard the assumption itself: if solo gating ever switches to weighted
    # confidence, the floor becomes a hard mute and this must be revisited.
    if "raw_confidence" not in src:
        fail("meta_agent no longer references raw_confidence — solo gates may "
             "now use WEIGHTED confidence, in which case MIN_AGENT_WEIGHT="
             f"{M.MIN_AGENT_WEIGHT} silently blocks every floored agent")
        return

    floor = M.MIN_AGENT_WEIGHT
    worst = max(floor * (1.0 - M.REGIME_PENALTY), 0.20)
    top = 1.15  # best weight any agent currently holds

    # Ranking check: a floored+penalised agent competing against the top
    # agent for MAX_SIGNALS_PER_TICK slots. If even a maximum-conviction
    # signal cannot outrank a mediocre one from a favoured agent, that
    # sleeve is effectively silent regardless of the solo bar.
    best_from_floored = 0.88 * worst
    mediocre_from_top = 0.66 * top
    if best_from_floored < mediocre_from_top:
        warn(f"ranking: a floored+penalised agent's BEST signal scores "
             f"{best_from_floored:.2f} vs {mediocre_from_top:.2f} for a "
             f"mediocre signal from the top-weighted agent. With "
             f"MAX_SIGNALS_PER_TICK={M.MAX_SIGNALS_PER_TICK}, that sleeve "
             f"rarely makes the cut even though the solo bar would admit it.")

    if M.REGIME_PENALTY >= 1.0:
        fail(f"REGIME_PENALTY={M.REGIME_PENALTY} — multiplicative penalty of "
             f"1.0 or more zeroes the weight entirely")


def check_exposure_gates():
    """The entry gates must admit some reachable book state."""
    import ensemble as E
    g, n = E.MAX_GROSS_LEVERAGE_ENTRY, E.MAX_NET_LONG_PCT
    if g <= 1.0:
        fail(f"MAX_GROSS_LEVERAGE_ENTRY={g} — no leveraged book can enter")
    if n <= 0.1:
        fail(f"MAX_NET_LONG_PCT={n} — effectively blocks all long entry")
    # Gross must leave room above net, or gross always binds first and the
    # net gate — the one calibrated against actual losses — never applies.
    if g < n + 0.5:
        warn(f"gross gate {g:.2f}x sits close to net gate {n:.0%}; a hedged "
             f"book will trip gross first and the net gate will never bind. "
             f"That is what deadlocked entries on 2026-08-14.")
    try:
        import invariants  # noqa
        if g >= 2.5:
            fail(f"entry gate {g:.2f}x is at or above the 2.5x leverage "
                 f"invariant — entries would be allowed into a CRITICAL state")
    except Exception:
        pass


def check_live_state():
    """Can anything actually trade right now? Reports, does not fail."""
    try:
        import os
        import requests
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parent / ".env")
        import ensemble as E
        from exposure import book_exposure
        h = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
             "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}
        P = "https://paper-api.alpaca.markets"
        eq = float(requests.get(f"{P}/v2/account", headers=h, timeout=15).json()["equity"])
        pos = requests.get(f"{P}/v2/positions", headers=h, timeout=15).json()
        g, n = book_exposure(pos)
        gl, nl = g / eq, n / eq
        lb = gl > E.MAX_GROSS_LEVERAGE_ENTRY or nl > E.MAX_NET_LONG_PCT
        print(f"  live: gross {gl:.2f}x/{E.MAX_GROSS_LEVERAGE_ENTRY:.2f}x  "
              f"net {nl:.0%}/{E.MAX_NET_LONG_PCT:.0%}  "
              f"longs {'BLOCKED' if lb else 'open'}")
        if lb:
            warn("longs are currently blocked by the exposure gate — "
                 "confirm this is intended, not another deadlock")
    except Exception as e:
        warn(f"live state check skipped: {e}")


def main() -> int:
    checks = [check_imports, check_regime_vocabulary, check_weight_arithmetic,
              check_exposure_gates, check_live_state]
    for c in checks:
        try:
            c()
        except Exception:
            fail(f"{c.__name__} raised:\n{traceback.format_exc()}")

    print()
    for w in WARNINGS:
        print(f"  [WARN] {w}")
    for f in FAILURES:
        print(f"  [FAIL] {f}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S) — do not deploy")
        return 1
    print(f"\nconfig OK ({len(WARNINGS)} warning(s)) — safe to deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
