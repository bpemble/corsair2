"""Trader-side quote-decision logic (Option B per mm_service_split).

Independent implementation that *intends* to match the broker's quote
engine. Not a refactor of the broker's path — purposefully separate so
parity drift is the signal we measure.

Inputs come from forwarded events: option price book, vol surface,
underlying. Outputs a structured decision per (strike, expiry, right,
side):

    {"action": "place" | "skip",
     "side": "BUY" | "SELL",
     "strike": float, "expiry": str, "right": str,
     "price": float | None,
     "theo": float | None,
     "iv": float | None,
     "reason": str}

What the broker does that we *don't* yet model (deliberate v2 simplifications):
    - peer-consensus cap engagement
    - inventory imbalance gates
    - layer-c cooldown
    - modify-storm guard
    - GTD lifetime / minimum-order-lifetime guards
    - active_orders incumbency (we'd amend if already resting)

Parity gap → next iteration. v2 captures the core "where would I quote"
question; the guards above mostly just skip-or-defer.
"""

from typing import Optional

# Rust hot-path pricing (already in production for the broker).
import corsair_pricing as _rs

# SABR implied-vol formula (Hagan 2002). Free function; no broker state.
from ..sabr import sabr_implied_vol, svi_implied_vol, time_to_expiry_years


def compute_theo(
    forward: float,
    strike: float,
    tte: float,
    right: str,
    vol_params: dict,
) -> Optional[tuple[float, float]]:
    """Compute (iv, theo) for one option given fitted vol params.

    Returns None if inputs are invalid or the surface model is unknown.
    """
    if forward <= 0 or strike <= 0 or tte <= 0:
        return None
    model = vol_params.get("model")
    try:
        if model == "sabr":
            iv = sabr_implied_vol(
                forward, strike, tte,
                float(vol_params["alpha"]),
                float(vol_params["beta"]),
                float(vol_params["rho"]),
                float(vol_params["nu"]),
            )
        elif model == "svi":
            iv = svi_implied_vol(
                forward, strike, tte,
                float(vol_params["a"]),
                float(vol_params["b"]),
                float(vol_params["rho"]),
                float(vol_params["m"]),
                float(vol_params["sigma"]),
            )
        else:
            return None
    except Exception:
        return None
    if iv is None or iv <= 0 or iv != iv:  # NaN
        return None
    theo = _rs.black76_price(forward, strike, tte, iv, 0.0, right)
    if theo is None or theo <= 0:
        return None
    return (float(iv), float(theo))


def decide(
    *,
    forward: float,
    strike: float,
    expiry: str,
    right: str,
    side: str,
    vol_params: dict,
    market_bid: Optional[float],
    market_ask: Optional[float],
    min_edge_ticks: int,
    tick_size: float,
    tte: Optional[float] = None,
) -> dict:
    """Make a single (strike, expiry, right, side) quote decision.

    Returns a dict with keys: action, side, strike, expiry, right,
    price, theo, iv, reason. Always returns a dict — never raises.
    """
    base = {
        "side": side, "strike": strike, "expiry": expiry, "right": right,
        "price": None, "theo": None, "iv": None,
    }
    if tte is None:
        try:
            tte = time_to_expiry_years(expiry)
        except Exception:
            return {**base, "action": "skip", "reason": "tte_calc_failed"}

    if not vol_params:
        return {**base, "action": "skip", "reason": "no_vol_surface"}

    res = compute_theo(forward, strike, tte, right, vol_params)
    if res is None:
        return {**base, "action": "skip", "reason": "theo_unavailable"}
    iv, theo = res
    base["theo"] = theo
    base["iv"] = iv

    edge = min_edge_ticks * tick_size

    if side == "BUY":
        target = theo - edge
        if target <= 0:
            return {**base, "action": "skip", "reason": "target_nonpositive"}
        if market_ask is not None and market_ask > 0 and target >= market_ask:
            # Crossing the existing ask — broker's logic would also skip.
            return {**base, "price": target, "action": "skip",
                    "reason": "would_cross_ask"}
        # Round to tick — broker's quote engine quantizes, we should too.
        target = round(target / tick_size) * tick_size
        return {**base, "price": target, "action": "place",
                "reason": "edge_below_theo"}
    elif side == "SELL":
        target = theo + edge
        if market_bid is not None and market_bid > 0 and target <= market_bid:
            return {**base, "price": target, "action": "skip",
                    "reason": "would_cross_bid"}
        target = round(target / tick_size) * tick_size
        return {**base, "price": target, "action": "place",
                "reason": "edge_above_theo"}
    else:
        return {**base, "action": "skip", "reason": "unknown_side"}
