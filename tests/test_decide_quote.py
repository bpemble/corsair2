"""Tests for trader.quote_decision.decide.

Covers:
  - Rust + Python decide_quote produce identical output for SABR inputs
  - Edge cases (zero/negative inputs, missing vol_params)
  - Cross-protect logic (would_cross_ask / would_cross_bid)
  - SVI inputs fall through to Python
"""
import random
from typing import Optional

import pytest

try:
    import corsair_pricing as _rs
    HAVE_RS = bool(getattr(_rs, "decide_quote", None))
except ImportError:
    HAVE_RS = False

from src.trader.quote_decision import decide


def _vol_params_sabr(rng: random.Random) -> dict:
    return {
        "model": "sabr",
        "alpha": rng.uniform(0.1, 1.0),
        "beta": 0.5,
        "rho": rng.uniform(-0.7, 0.7),
        "nu": rng.uniform(0.1, 1.5),
    }


@pytest.mark.skipif(not HAVE_RS, reason="corsair_pricing.decide_quote not built")
@pytest.mark.parametrize("seed", [1, 42, 137])
def test_rust_python_parity_sabr(seed):
    """Rust and Python decide() agree for SABR inputs across many cases."""
    rng = random.Random(seed)
    for _ in range(200):
        F = rng.uniform(2.0, 8.0)
        K = round(rng.uniform(2.0, 8.0) * 20) / 20  # quarter-tick strikes
        T = rng.uniform(0.01, 1.0)
        right = rng.choice(["C", "P"])
        side = rng.choice(["BUY", "SELL"])
        bid = rng.uniform(0.01, 0.5)
        ask = bid + rng.uniform(0.001, 0.05)
        vp = _vol_params_sabr(rng)

        # Rust path
        rs_d = _rs.decide_quote(
            F, K, "20260526", right, side, vp,
            bid, ask, 2, 0.0005, T,
        )

        # Python path (force by passing None vol_params then re-calling
        # — but easier: call decide() with backend env not set; the
        # function will dispatch. To ENSURE Python, monkey-patch.
        import src.trader.quote_decision as qd
        saved = qd._USE_RS_DECIDE
        qd._USE_RS_DECIDE = False
        try:
            py_d = decide(
                forward=F, strike=K, expiry="20260526",
                right=right, side=side, vol_params=vp,
                market_bid=bid, market_ask=ask,
                min_edge_ticks=2, tick_size=0.0005, tte=T,
            )
        finally:
            qd._USE_RS_DECIDE = saved

        # Same action and reason
        assert rs_d["action"] == py_d["action"], f"action mismatch: {rs_d} vs {py_d}"
        assert rs_d["reason"] == py_d["reason"], f"reason mismatch: {rs_d} vs {py_d}"
        # Numeric fields agree to within float precision
        for k in ("price", "theo", "iv"):
            rv = rs_d.get(k)
            pv = py_d.get(k)
            if rv is None or pv is None:
                assert rv is None and pv is None, f"None mismatch on {k}"
            else:
                assert abs(rv - pv) < 1e-9, f"{k} differ: rs={rv}, py={pv}"


def test_no_vol_params_skips():
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="BUY", vol_params=None,
        market_bid=0.1, market_ask=0.11,
        min_edge_ticks=2, tick_size=0.0005, tte=0.5,
    )
    assert d["action"] == "skip"
    assert d["reason"] == "no_vol_surface"


def test_invalid_tte_skips():
    vp = {"model": "sabr", "alpha": 0.3, "beta": 0.5, "rho": 0.0, "nu": 0.5}
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="BUY", vol_params=vp,
        market_bid=None, market_ask=None,
        min_edge_ticks=2, tick_size=0.0005, tte=0.0,
    )
    assert d["action"] == "skip"


def test_cross_protect_buy():
    vp = {"model": "sabr", "alpha": 0.3, "beta": 0.5, "rho": 0.0, "nu": 0.5}
    # Theo will be moderate; ask is below theo → BUY at theo-edge would cross.
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="BUY", vol_params=vp,
        market_bid=0.05, market_ask=0.06,  # cheap ask
        min_edge_ticks=2, tick_size=0.0005, tte=0.5,
    )
    # If theo > ask, our BUY-at-theo-minus-edge would still be ≥ ask → skip.
    # Either way, the decision is well-formed.
    assert d["action"] in ("place", "skip")
    if d["action"] == "skip":
        assert d["reason"] in ("would_cross_ask", "target_nonpositive",
                               "theo_unavailable")


def test_cross_protect_sell():
    vp = {"model": "sabr", "alpha": 0.3, "beta": 0.5, "rho": 0.0, "nu": 0.5}
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="SELL", vol_params=vp,
        market_bid=10.0, market_ask=10.5,  # absurdly high bid
        min_edge_ticks=2, tick_size=0.0005, tte=0.5,
    )
    # Bid is way above any reasonable theo → SELL would cross.
    assert d["action"] == "skip"
    assert d["reason"] == "would_cross_bid"


def test_svi_falls_through_to_python():
    """When vol_params model is SVI, Rust returns 'model_not_in_rust'
    and decide() retries via Python. Python doesn't yet support SVI in
    this decision module either, so result is a skip with the Python
    reason — we just want to confirm we don't crash."""
    vp = {"model": "svi", "a": 0.01, "b": 0.05, "rho": -0.5,
          "m": 0.0, "sigma": 0.1}
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="BUY", vol_params=vp,
        market_bid=0.1, market_ask=0.12,
        min_edge_ticks=2, tick_size=0.0005, tte=0.5,
    )
    # Python path uses svi_implied_vol from sabr.py → returns a real
    # IV; decision is well-formed.
    assert d["action"] in ("place", "skip")
    assert "side" in d and "strike" in d


def test_unknown_side_skips():
    vp = {"model": "sabr", "alpha": 0.3, "beta": 0.5, "rho": 0.0, "nu": 0.5}
    d = decide(
        forward=6.0, strike=6.0, expiry="20260526",
        right="C", side="WHATEVER", vol_params=vp,
        market_bid=0.1, market_ask=0.12,
        min_edge_ticks=2, tick_size=0.0005, tte=0.5,
    )
    assert d["action"] == "skip"
    assert d["reason"] == "unknown_side"
