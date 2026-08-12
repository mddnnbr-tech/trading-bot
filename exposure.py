"""
exposure.py
───────────
Signed market exposure of a position, in equity-equivalent dollars.

Why this is not just `market_value`. On 2026-08-12 the book held SPY long
AND SQQQ long. Naive arithmetic read that as +$16,640 of long exposure. In
economic terms SQQQ is a 3x INVERSE Nasdaq fund: $8,142 of it carries about
-$24,400 of directional exposure. The book was therefore far less net-long
than market_value implied, and a gate reading market_value would both
misjudge the risk and refuse entries for the wrong reason.

Leveraged and inverse ETFs are the only instruments where notional and
market value diverge like this, so a small explicit table beats inferring
it. Anything absent from the table behaves normally: exposure == value.

Note these funds reset leverage daily and decay when held — the multiplier
describes today's directional risk, not a durable position value.
"""

from __future__ import annotations

# symbol -> exposure multiplier applied to market value.
# Negative = inverse (profits when the underlying falls).
LEVERAGED_ETFS: dict[str, float] = {
    # 3x inverse
    "SQQQ": -3.0, "SPXS": -3.0, "SDOW": -3.0, "SOXS": -3.0,
    "TZA": -3.0, "LABD": -3.0, "TECS": -3.0, "FAZ": -3.0,
    # 2x inverse
    "QID": -2.0, "SDS": -2.0, "DXD": -2.0, "TWM": -2.0,
    # 1x inverse
    "SH": -1.0, "PSQ": -1.0, "DOG": -1.0, "RWM": -1.0,
    # 3x long
    "TQQQ": 3.0, "UPRO": 3.0, "UDOW": 3.0, "SOXL": 3.0,
    "TNA": 3.0, "LABU": 3.0, "TECL": 3.0, "FAS": 3.0,
    # 2x long
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "UWM": 2.0,
}


def signed_exposure(symbol: str, market_value: float) -> float:
    """Directional exposure in dollars. Short positions already carry a
    negative market_value, so the multiplier composes correctly with them."""
    return market_value * LEVERAGED_ETFS.get(str(symbol).upper(), 1.0)


def book_exposure(positions) -> tuple[float, float]:
    """(gross, net) exposure in dollars over an Alpaca positions list.

    Accepts either dicts (REST) or SDK objects (alpaca-py).
    """
    gross = net = 0.0
    for p in positions:
        if isinstance(p, dict):
            sym, mv = p.get("symbol", ""), float(p.get("market_value") or 0)
        else:
            sym, mv = str(p.symbol), float(p.market_value)
        e = signed_exposure(sym, mv)
        gross += abs(e)
        net += e
    return gross, net
