"""
auto_tune.py
─────────────
The step that makes the loop autonomous: reads the post-mortem's root
causes and WRITES the parameter changes itself, with no human in the
path. Runs Sunday 06:00 ET on the VM.

Before this, the chain was:
    trade -> classify -> report -> [HUMAN reads it] -> code change
The human was the bottleneck. Now:
    trade -> classify -> tune -> agents read tuning.json next tick

Guardrails, because an unsupervised optimizer is how accounts die:
  • ONE parameter changes per run — never several at once, or you can't
    attribute the result to a cause.
  • Every parameter is hard-bounded; no runaway drift.
  • A change requires >=8 classified trades and >=25% share of that
    failure mode. Noise cannot move a global setting.
  • Every change is journalled with its evidence to tuning_history.jsonl.
  • If equity fell over the week following a change, the change is
    REVERTED and the parameter frozen for 3 weeks.

Nothing here can place a trade, size a position, or disable a risk limit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("AutoTune")
BASE = Path(__file__).resolve().parent
TUNING = BASE / "data" / "tuning.json"
HISTORY = BASE / "data" / "tuning_history.jsonl"

# name -> (default, min, max, step). Bounds are the safety contract.
PARAMS = {
    "atr_stop_mult":          (1.5,  1.2, 2.5,  0.15),
    "min_solo_confidence":    (0.65, 0.58, 0.80, 0.03),
    "options_min_confidence": (0.70, 0.60, 0.85, 0.05),
    "daily_trade_cap":        (4,    2,   8,    1),
}


def load() -> dict:
    cfg = {k: v[0] for k, v in PARAMS.items()}
    try:
        if TUNING.exists():
            saved = json.loads(TUNING.read_text()) or {}
            for k, v in saved.items():
                if k in PARAMS:
                    lo, hi = PARAMS[k][1], PARAMS[k][2]
                    cfg[k] = min(max(v, lo), hi)      # clamp on read too
    except Exception as e:
        log.warning(f"tuning read failed, using defaults: {e}")
    return cfg


def _journal(rec: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _equity() -> float | None:
    try:
        import os, requests
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
        h = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
             "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}
        r = requests.get("https://paper-api.alpaca.markets/v2/account",
                         headers=h, timeout=15).json()
        return float(r.get("equity") or 0)
    except Exception:
        return None


def _recent_changes(weeks: int = 3) -> list[dict]:
    cut = (datetime.now() - timedelta(weeks=weeks)).isoformat()
    out = []
    try:
        for line in HISTORY.read_text().splitlines():
            r = json.loads(line)
            if r.get("ts", "") >= cut and r.get("action") == "change":
                out.append(r)
    except Exception:
        pass
    return out


def _check_last_change(cfg: dict, eq_now: float | None) -> bool:
    """Revert the previous change if equity fell since. Returns True if a
    revert happened (which consumes this run's single change)."""
    prior = _recent_changes(2)
    if not prior or eq_now is None:
        return False
    last = prior[-1]
    if last.get("verified"):
        return False
    try:
        age_days = (datetime.now() - datetime.fromisoformat(last["ts"])).days
    except Exception:
        return False
    if age_days < 6:
        return False

    before = last.get("equity_at_change")
    if before and eq_now < before:
        p, old = last["param"], last["from"]
        cfg[p] = old
        save(cfg)
        _journal({"ts": datetime.now().isoformat(timespec="seconds"),
                  "action": "revert", "param": p, "to": old,
                  "reason": f"equity fell {eq_now - before:+,.0f} in the week after the change",
                  "frozen_until": (datetime.now() + timedelta(weeks=3)).isoformat()})
        log.warning(f"AUTO-TUNE REVERT: {p} -> {old} (equity {eq_now-before:+,.0f})")
        return True

    last["verified"] = True
    _journal({"ts": datetime.now().isoformat(timespec="seconds"),
              "action": "verify", "param": last["param"],
              "result": f"equity {eq_now - (before or eq_now):+,.0f} — change kept"})
    return False


def _frozen() -> set:
    out = set()
    now = datetime.now().isoformat()
    try:
        for line in HISTORY.read_text().splitlines():
            r = json.loads(line)
            if r.get("action") == "revert" and r.get("frozen_until", "") > now:
                out.add(r["param"])
    except Exception:
        pass
    return out


def save(cfg: dict) -> None:
    TUNING.parent.mkdir(parents=True, exist_ok=True)
    TUNING.write_text(json.dumps(cfg, indent=2))


def tune() -> dict:
    import trade_context
    cfg = load()
    eq = _equity()

    if _check_last_change(cfg, eq):
        return cfg                     # a revert is this run's one change

    trade_context.classify_closed_trades(limit=400)
    summary = trade_context.failure_summary(20)
    total = sum(s["count"] for s in summary) or 1
    frozen = _frozen()

    # Dominant loss mode -> its one bounded remedy
    losses = sorted((s for s in summary if s["pnl"] < 0), key=lambda s: s["pnl"])
    for s in losses:
        mode, n, share = s["mode"], s["count"], s["count"] / total
        if n < 8 or share < 0.25:
            continue
        param, direction = {
            "STOP_TOO_TIGHT":  ("atr_stop_mult", +1),
            "NO_CATALYST":     ("min_solo_confidence", +1),
            "GAP_LOSS":        ("options_min_confidence", -1),   # route MORE to options
            "WRONG_DIRECTION": ("daily_trade_cap", -1),          # trade less when entries are bad
        }.get(mode, (None, 0))
        if not param or param in frozen:
            continue

        default, lo, hi, step = PARAMS[param]
        old = cfg[param]
        new = min(max(old + direction * step, lo), hi)
        if abs(new - old) < 1e-9:
            continue

        cfg[param] = round(new, 4) if isinstance(new, float) else int(new)
        save(cfg)
        _journal({"ts": datetime.now().isoformat(timespec="seconds"),
                  "action": "change", "param": param, "from": old, "to": cfg[param],
                  "mode": mode, "trades": n, "share": round(share, 3),
                  "mode_pnl": s["pnl"], "equity_at_change": eq,
                  "reason": f"{mode} is {share:.0%} of classified trades "
                            f"({n} trades, ${s['pnl']:,.0f}) — {s['remedy']}"})
        log.warning(f"AUTO-TUNE: {param} {old} -> {cfg[param]}  ({mode}, "
                    f"{n} trades, ${s['pnl']:,.0f})")
        return cfg

    _journal({"ts": datetime.now().isoformat(timespec="seconds"),
              "action": "noop", "reason": "no failure mode met the "
                                          ">=8 trades and >=25% share bar"})
    log.info("AUTO-TUNE: no dominant pattern — nothing changed")
    return cfg


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("config after tune:", json.dumps(tune(), indent=2))
