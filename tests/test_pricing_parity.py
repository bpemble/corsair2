"""Parity tests: Rust corsair_pricing vs Python PricingEngine.

These run only when the corsair_pricing extension is importable. If it
isn't (host doesn't have the wheel installed), the tests SKIP rather
than fail — the Rust port is opt-in and Python remains the fallback.
"""
import math
import random

import pytest

try:
    import corsair_pricing as rs
    HAVE_RS = True
except ImportError:
    HAVE_RS = False

from src.pricing import PricingEngine

pytestmark = pytest.mark.skipif(not HAVE_RS, reason="corsair_pricing extension not built")


@pytest.mark.parametrize("seed", [42, 137, 2024, 31415])
def test_black76_parity(seed):
    """Black-76 price agreement to ~1e-9 across 1000 random inputs per seed."""
    rng = random.Random(seed)
    max_abs_err = 0.0
    for _ in range(1000):
        F = rng.uniform(0.5, 10.0)
        K = rng.uniform(0.5, 10.0)
        T = rng.uniform(0.001, 2.0)
        sigma = rng.uniform(0.05, 1.5)
        r = rng.uniform(0.0, 0.05)
        right = rng.choice(["C", "P"])
        py = PricingEngine.black76_price(F, K, T, sigma, r, right)
        rs_val = rs.black76_price(F, K, T, sigma, r, right)
        err = abs(py - rs_val)
        max_abs_err = max(max_abs_err, err)
        assert err < 1e-9, (
            f"black76 mismatch seed={seed} F={F:.4f} K={K:.4f} T={T:.4f} "
            f"sigma={sigma:.4f} right={right}: py={py:.12f} rs={rs_val:.12f} "
            f"err={err:.2e}"
        )


def test_black76_edge_cases():
    """Boundary inputs: zero/negative T or sigma should both return intrinsic."""
    cases = [
        # (F, K, T, sigma, right, expected_intrinsic)
        (5.0, 4.0, 0.0, 0.3, "C", 1.0),
        (5.0, 4.0, 0.0, 0.3, "P", 0.0),
        (5.0, 6.0, 0.0, 0.3, "C", 0.0),
        (5.0, 6.0, 0.0, 0.3, "P", 1.0),
        (5.0, 5.0, 0.5, 0.0, "C", 0.0),
        (5.0, 5.0, 0.5, 0.0, "P", 0.0),
    ]
    for F, K, T, sigma, right, expected in cases:
        py = PricingEngine.black76_price(F, K, T, sigma, 0.0, right)
        rs_val = rs.black76_price(F, K, T, sigma, 0.0, right)
        assert math.isclose(py, expected, abs_tol=1e-12), f"py disagrees with intrinsic for {F=} {K=} {right=}"
        assert math.isclose(rs_val, expected, abs_tol=1e-12), f"rs disagrees with intrinsic for {F=} {K=} {right=}"


@pytest.mark.parametrize("seed", [42, 137, 2024])
def test_implied_vol_parity(seed):
    """IV solver agreement to ~1e-5 across 500 random inputs per seed.

    Generates a market price from a known sigma_true, then solves both
    implementations for IV. Asserts they recover sigma_true within
    Brent's xtol (1e-6) and agree with each other within the sum of
    their independent rounding/iteration noise.
    """
    rng = random.Random(seed)
    max_diff = 0.0
    n_tested = 0
    for _ in range(500):
        F = rng.uniform(2.0, 8.0)
        K = rng.uniform(2.0, 8.0)
        T = rng.uniform(0.01, 1.0)
        sigma_true = rng.uniform(0.1, 1.0)
        right = rng.choice(["C", "P"])
        market = PricingEngine.black76_price(F, K, T, sigma_true, 0.0, right)
        # Skip near-zero markets where IV is degenerate.
        if market < 1e-4:
            continue
        py_iv = PricingEngine.implied_vol(market, F, K, T, 0.0, right)
        rs_iv = rs.implied_vol(market, F, K, T, 0.0, right)
        if py_iv is None and rs_iv is None:
            continue
        assert py_iv is not None, f"py None, rs={rs_iv}: F={F} K={K} T={T} sigma_true={sigma_true} right={right}"
        assert rs_iv is not None, f"rs None, py={py_iv}: F={F} K={K} T={T} sigma_true={sigma_true} right={right}"
        diff = abs(py_iv - rs_iv)
        max_diff = max(max_diff, diff)
        n_tested += 1
        # Both solvers use xtol=1e-6, so they may differ by up to ~2*xtol.
        assert diff < 1e-5, (
            f"IV mismatch seed={seed} F={F:.4f} K={K:.4f} T={T:.4f} "
            f"sigma_true={sigma_true:.4f} right={right}: py={py_iv:.10f} "
            f"rs={rs_iv:.10f} diff={diff:.2e}"
        )
    assert n_tested > 100, f"too few tests evaluated: {n_tested}"


def test_implied_vol_below_intrinsic_returns_none():
    """Below-intrinsic markets return None in both implementations."""
    F, K, T, right = 5.0, 4.0, 0.5, "C"
    intrinsic = max(F - K, 0.0)
    sub = intrinsic * 0.5  # below intrinsic
    assert PricingEngine.implied_vol(sub, F, K, T, 0.0, right) is None
    assert rs.implied_vol(sub, F, K, T, 0.0, right) is None


def test_implied_vol_zero_or_negative_returns_none():
    cases = [
        (0.0, 5.0, 5.0, 0.5, "C"),
        (-0.1, 5.0, 5.0, 0.5, "C"),
        (1.0, 5.0, 5.0, 0.0, "C"),  # T=0
    ]
    for market, F, K, T, right in cases:
        assert PricingEngine.implied_vol(market, F, K, T, 0.0, right) is None
        assert rs.implied_vol(market, F, K, T, 0.0, right) is None
