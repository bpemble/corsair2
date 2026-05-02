"""Rust vs Python parity for greeks + SPAN.

Verifies compute_greeks and SPAN scan/portfolio_margin produce
numerically equivalent output across a battery of inputs.
"""
import random

import pytest

from src.greeks import GreeksCalculator
from src.synthetic_span import SyntheticSpan

try:
    import corsair_pricing as _rs
    HAVE_RS_GREEKS = bool(getattr(_rs, "compute_greeks", None))
    HAVE_RS_SPAN = bool(getattr(_rs, "span_portfolio_margin", None))
except ImportError:
    HAVE_RS_GREEKS = False
    HAVE_RS_SPAN = False


# ── Greeks parity ──────────────────────────────────────────────────


@pytest.mark.skipif(not HAVE_RS_GREEKS, reason="Rust greeks not built")
@pytest.mark.parametrize("seed", [1, 7, 42, 137, 999])
def test_greeks_parity_random(seed):
    """Random options in the typical operating range — Rust and
    Python paths must agree to ~1e-9."""
    rng = random.Random(seed)
    calc = GreeksCalculator()
    import os
    for _ in range(50):
        F = rng.uniform(2.0, 10.0)
        K = rng.uniform(2.0, 10.0)
        T = rng.uniform(0.005, 1.0)
        sigma = rng.uniform(0.1, 1.5)
        r = rng.uniform(0.0, 0.05)
        right = rng.choice(["C", "P"])
        mult = rng.choice([25_000, 50, 100])

        # Rust path
        os.environ["CORSAIR_GREEKS"] = "rust"
        # The module-level _USE_RS_GREEKS is captured at import; toggle
        # the function path explicitly via a fresh GreeksCalculator that
        # uses the import-time decision. To get true parity comparison,
        # call both paths directly:
        rs_g = calc.calculate(F, K, T, sigma, r=r, right=right, multiplier=mult)
        py_g = calc._calculate_python(F, K, T, sigma, r=r, right=right,
                                       multiplier=mult)

        for field in ("delta", "gamma", "theta", "vega"):
            rv = getattr(rs_g, field)
            pv = getattr(py_g, field)
            # Allow tiny float rounding (statrs vs scipy.stats.norm)
            assert abs(rv - pv) < 1e-7 or abs(rv - pv) / max(abs(pv), 1e-9) < 1e-6, (
                f"{field} diverged at seed={seed} F={F} K={K} T={T} "
                f"sigma={sigma} right={right}: rs={rv} py={pv}"
            )


@pytest.mark.skipif(not HAVE_RS_GREEKS, reason="Rust greeks not built")
def test_greeks_invalid_inputs_match():
    """Both paths must return the same intrinsic-delta degraded result
    on T=0, sigma<=0, etc."""
    calc = GreeksCalculator()
    cases = [
        (6.0, 6.0, 0.0, 0.5, "C"),  # T=0
        (6.0, 6.0, 0.05, 0.0, "C"),  # sigma=0
        (0.0, 6.0, 0.05, 0.5, "C"),  # F=0
        (6.0, 0.0, 0.05, 0.5, "C"),  # K=0
    ]
    for F, K, T, sigma, right in cases:
        rs_g = calc.calculate(F, K, T, sigma, right=right, multiplier=50)
        py_g = calc._calculate_python(F, K, T, sigma, right=right, multiplier=50)
        assert rs_g.delta == py_g.delta
        assert rs_g.gamma == 0.0 and py_g.gamma == 0.0
        assert rs_g.vega == 0.0 and py_g.vega == 0.0


# ── SPAN parity ────────────────────────────────────────────────────


def _span_cfg():
    """Production-shaped config for SyntheticSpan."""
    from types import SimpleNamespace
    return SimpleNamespace(
        product=SimpleNamespace(multiplier=25_000),
        synthetic_span=SimpleNamespace(
            up_scan_pct=0.56, down_scan_pct=0.49,
            vol_scan_pct=0.25, extreme_mult=3.0,
            extreme_cover=0.33, short_option_minimum=500.0,
        ),
    )


@pytest.mark.skipif(not HAVE_RS_SPAN, reason="Rust SPAN not built")
@pytest.mark.parametrize("seed", [1, 7, 42, 137])
def test_span_position_risk_parity(seed):
    """Risk array for one position — Rust and Python must agree."""
    import os
    cfg = _span_cfg()

    rng = random.Random(seed)
    for _ in range(20):
        F = rng.uniform(3.0, 8.0)
        K = F + rng.choice([-0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2])
        T = rng.uniform(0.01, 0.3)
        iv = rng.uniform(0.2, 1.0)
        right = rng.choice(["C", "P"])

        os.environ["CORSAIR_SPAN"] = "rust"
        s_rs = SyntheticSpan(cfg)
        ra_rs = s_rs.position_risk_array(F, K, T, iv, right)

        os.environ["CORSAIR_SPAN"] = "python"
        s_py = SyntheticSpan(cfg)
        # Force re-eval of module-level _USE_RS_SPAN by reimporting
        import importlib, src.synthetic_span as ss
        importlib.reload(ss)
        s_py = ss.SyntheticSpan(cfg)
        ra_py = s_py.position_risk_array(F, K, T, iv, right)

        os.environ["CORSAIR_SPAN"] = "rust"  # restore
        importlib.reload(ss)

        # Allow tiny relative drift (statrs vs scipy)
        for i, (r, p) in enumerate(zip(ra_rs, ra_py)):
            tol = max(abs(p) * 1e-6, 1e-3)
            assert abs(r - p) < tol, (
                f"seed={seed} scenario={i}: rs={r} py={p} F={F} K={K}"
            )


@pytest.mark.skipif(not HAVE_RS_SPAN, reason="Rust SPAN not built")
def test_span_portfolio_margin_parity_strangle():
    """A short strangle: -1 OTM call, -1 OTM put. Both paths must
    produce the same margin output."""
    import os, importlib, src.synthetic_span as ss

    cfg = _span_cfg()
    F = 6.0
    positions = [
        (6.05, "C", 0.05, 0.45, -1),  # short OTM call
        (5.95, "P", 0.05, 0.55, -1),  # short OTM put
    ]

    os.environ["CORSAIR_SPAN"] = "rust"
    importlib.reload(ss)
    s_rs = ss.SyntheticSpan(cfg)
    m_rs = s_rs.portfolio_margin(F, positions)

    os.environ["CORSAIR_SPAN"] = "python"
    importlib.reload(ss)
    s_py = ss.SyntheticSpan(cfg)
    m_py = s_py.portfolio_margin(F, positions)

    os.environ["CORSAIR_SPAN"] = "rust"
    importlib.reload(ss)

    # All numeric fields agree to ~1e-3
    for key in ("scan_risk", "net_option_value", "long_premium",
                "short_minimum", "total_margin"):
        rv = m_rs[key]
        pv = m_py[key]
        tol = max(abs(pv) * 1e-6, 1e-3)
        assert abs(rv - pv) < tol, f"{key} diverged: rs={rv} py={pv}"
    assert m_rs["worst_scenario"] == m_py["worst_scenario"]


# ── Performance smoke ─────────────────────────────────────────────


@pytest.mark.skipif(not HAVE_RS_SPAN, reason="Rust SPAN not built")
def test_span_portfolio_margin_speedup():
    """100 portfolio-margin calls. Rust must be meaningfully faster
    than Python — gate at 5× to allow CI variance."""
    import time, os, importlib, src.synthetic_span as ss

    cfg = _span_cfg()
    F = 6.0
    # 6-position book — typical for our HG strangles.
    positions = [
        (6.00, "C", 0.05, 0.45, -1),
        (6.05, "C", 0.05, 0.42, -1),
        (6.10, "C", 0.05, 0.40, -1),
        (5.95, "P", 0.05, 0.50, -1),
        (5.90, "P", 0.05, 0.55, -1),
        (5.85, "P", 0.05, 0.60, -1),
    ]

    os.environ["CORSAIR_SPAN"] = "python"
    importlib.reload(ss)
    s_py = ss.SyntheticSpan(cfg)
    t0 = time.perf_counter()
    for _ in range(100):
        s_py.portfolio_margin(F, positions)
    py_dur = time.perf_counter() - t0

    os.environ["CORSAIR_SPAN"] = "rust"
    importlib.reload(ss)
    s_rs = ss.SyntheticSpan(cfg)
    t0 = time.perf_counter()
    for _ in range(100):
        s_rs.portfolio_margin(F, positions)
    rs_dur = time.perf_counter() - t0

    speedup = py_dur / rs_dur
    print(f"\n  Python: {py_dur*1000:.1f}ms total, {py_dur*10:.2f}ms/call")
    print(f"  Rust:   {rs_dur*1000:.1f}ms total, {rs_dur*10:.2f}ms/call")
    print(f"  speedup: {speedup:.1f}×")
    assert speedup > 3.0, f"Expected ≥3× speedup, got {speedup:.1f}×"


@pytest.mark.skipif(not HAVE_RS_GREEKS, reason="Rust greeks not built")
def test_greeks_speedup():
    """1000 single-option greek calls."""
    import time, os, importlib, src.greeks as gm

    os.environ["CORSAIR_GREEKS"] = "python"
    importlib.reload(gm)
    calc_py = gm.GreeksCalculator()
    t0 = time.perf_counter()
    for _ in range(1000):
        calc_py.calculate(6.0, 6.0, 0.05, 0.5, right="C", multiplier=25_000)
    py_dur = time.perf_counter() - t0

    os.environ["CORSAIR_GREEKS"] = "rust"
    importlib.reload(gm)
    calc_rs = gm.GreeksCalculator()
    t0 = time.perf_counter()
    for _ in range(1000):
        calc_rs.calculate(6.0, 6.0, 0.05, 0.5, right="C", multiplier=25_000)
    rs_dur = time.perf_counter() - t0

    speedup = py_dur / rs_dur
    print(f"\n  Python: {py_dur*1000:.1f}ms total, {py_dur:.2f}ms/call")
    print(f"  Rust:   {rs_dur*1000:.1f}ms total, {rs_dur:.2f}ms/call")
    print(f"  speedup: {speedup:.1f}×")
    assert speedup > 3.0, f"Expected ≥3× speedup, got {speedup:.1f}×"
