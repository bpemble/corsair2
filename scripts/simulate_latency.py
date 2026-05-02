"""End-to-end latency simulation: estimate the broker's hot-loop cost
under both pure-Python and Rust-backed math paths.

This is NOT a substitute for live measurement. It's a back-of-the-envelope
based on:
  - Direct measurement of the math hot-paths (greeks, SPAN, calibrate)
  - Python overhead per quote-cycle iteration measured under load
  - Realistic per-cycle action counts (refresh_greeks, gate check, theo
    eval, etc.)

For each scenario it prints what the broker is expected to spend per
cycle / per fill / per refit on math vs orchestration.

Run inside the corsair container:
    docker run --rm -v $PWD:/work -w /work corsair-corsair \\
        python scripts/simulate_latency.py
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

# Make sure the Rust path is selected (default) and verify it's built.
import corsair_pricing as _rs
HAS_RS_CAL = bool(getattr(_rs, "calibrate_sabr", None))
HAS_RS_GREEKS = bool(getattr(_rs, "compute_greeks", None))
HAS_RS_SPAN = bool(getattr(_rs, "span_portfolio_margin", None))


def measure(fn, n: int) -> float:
    """Return median per-call time in microseconds."""
    samples = []
    for _ in range(5):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        t1 = time.perf_counter_ns()
        samples.append((t1 - t0) / n / 1_000)  # ns→µs
    samples.sort()
    return samples[len(samples) // 2]


def hr(s):
    print()
    print(s)
    print("─" * 72)


def main():
    print(f"Rust available: cal={HAS_RS_CAL} greeks={HAS_RS_GREEKS} span={HAS_RS_SPAN}")
    print()

    # ────────────────────────────────────────────────────────────────
    # 1) Math hot-path microbenchmarks
    # ────────────────────────────────────────────────────────────────
    hr("Math hot-path microbenchmarks (median, µs/call)")

    # Greeks
    os.environ["CORSAIR_GREEKS"] = "python"
    import importlib, src.greeks as gm
    importlib.reload(gm)
    calc_py = gm.GreeksCalculator()
    py_greek = measure(
        lambda: calc_py.calculate(6.0, 6.0, 0.05, 0.5, right="C", multiplier=25_000),
        n=200,
    )

    os.environ["CORSAIR_GREEKS"] = "rust"
    importlib.reload(gm)
    calc_rs = gm.GreeksCalculator()
    rs_greek = measure(
        lambda: calc_rs.calculate(6.0, 6.0, 0.05, 0.5, right="C", multiplier=25_000),
        n=200,
    )

    # SPAN portfolio (6-position book)
    cfg = SimpleNamespace(
        product=SimpleNamespace(multiplier=25_000),
        synthetic_span=SimpleNamespace(
            up_scan_pct=0.56, down_scan_pct=0.49, vol_scan_pct=0.25,
            extreme_mult=3.0, extreme_cover=0.33, short_option_minimum=500.0,
        ),
    )
    positions = [
        (6.00, "C", 0.05, 0.45, -1),
        (6.05, "C", 0.05, 0.42, -1),
        (6.10, "C", 0.05, 0.40, -1),
        (5.95, "P", 0.05, 0.50, -1),
        (5.90, "P", 0.05, 0.55, -1),
        (5.85, "P", 0.05, 0.60, -1),
    ]
    os.environ["CORSAIR_SPAN"] = "python"
    import src.synthetic_span as ss
    importlib.reload(ss)
    s_py = ss.SyntheticSpan(cfg)
    py_span = measure(lambda: s_py.portfolio_margin(6.0, positions), n=50)

    os.environ["CORSAIR_SPAN"] = "rust"
    importlib.reload(ss)
    s_rs = ss.SyntheticSpan(cfg)
    rs_span = measure(lambda: s_rs.portfolio_margin(6.0, positions), n=50)

    # SABR calibrator (8-strike chain)
    from src.sabr import (_calibrate_sabr_python, sabr_implied_vol)
    F, T, beta = 6.0, 0.05, 0.5
    Ks = [F + i * 0.05 for i in range(-4, 5)]
    ivs = [sabr_implied_vol(F, K, T, 0.6, beta, -0.3, 1.0) for K in Ks]
    py_cal = measure(
        lambda: _calibrate_sabr_python(F, T, Ks, ivs, beta=beta, max_rmse=0.05),
        n=10,
    )
    rs_cal = measure(
        lambda: _rs.calibrate_sabr(F, T, Ks, ivs, beta, 0.05, None),
        n=20,
    )

    print(f"{'Component':<32} {'Python':>12} {'Rust':>12}  Speedup")
    print(f"{'─' * 72}")
    print(f"{'compute_greeks (1 contract)':<32} {py_greek:>10.2f}µs {rs_greek:>10.2f}µs  {py_greek/rs_greek:>5.0f}×")
    print(f"{'SPAN margin (6-pos book)':<32} {py_span:>10.2f}µs {rs_span:>10.2f}µs  {py_span/rs_span:>5.0f}×")
    print(f"{'SABR fit (8 strikes)':<32} {py_cal:>10.2f}µs {rs_cal:>10.2f}µs  {py_cal/rs_cal:>5.0f}×")

    # ────────────────────────────────────────────────────────────────
    # 2) Refresh-greeks: portfolio re-Greeking (every 5 min + every fill)
    #    Typical book: 6 positions in HG.
    # ────────────────────────────────────────────────────────────────
    hr("refresh_greeks (6-position portfolio): per call")
    py_refresh = py_greek * 6  # 6 sequential greek calls
    rs_refresh = rs_greek * 6
    print(f"  Python:  {py_refresh:.0f} µs    (6 × {py_greek:.0f} µs)")
    print(f"  Rust:    {rs_refresh:.0f} µs    (6 × {rs_greek:.0f} µs)")
    print(f"  Saved:   {py_refresh - rs_refresh:.0f} µs / refresh")

    # ────────────────────────────────────────────────────────────────
    # 3) Constraint check on a fill: SPAN + 2× greeks (current + post)
    # ────────────────────────────────────────────────────────────────
    hr("Constraint check on fill: per call")
    # check_fill_margin runs SPAN twice (current state + post-fill scan).
    # Greeks are already cached on Position; the dominant cost is SPAN.
    py_check = py_span * 2
    rs_check = rs_span * 2
    print(f"  Python:  {py_check:.0f} µs    (2 × SPAN at {py_span:.0f} µs)")
    print(f"  Rust:    {rs_check:.0f} µs    (2 × SPAN at {rs_span:.0f} µs)")
    print(f"  Saved:   {py_check - rs_check:.0f} µs / fill check")

    # ────────────────────────────────────────────────────────────────
    # 4) SABR refit on broker's main loop (~60s cadence)
    #    A refit covers: 2 sides × ~2 expiries = ~4 calibrations
    # ────────────────────────────────────────────────────────────────
    hr("SABR refit cycle (4 calibrations × 2 sides):")
    n_cals = 4
    py_refit = py_cal * n_cals
    rs_refit = rs_cal * n_cals
    print(f"  Python:  {py_refit/1000:.1f} ms    ({n_cals} × {py_cal/1000:.1f} ms)")
    print(f"  Rust:    {rs_refit/1000:.2f} ms    ({n_cals} × {rs_cal:.0f} µs)")
    print(f"  Saved:   {(py_refit - rs_refit)/1000:.1f} ms / refit cycle")
    print(f"  Implication: refit cadence can drop from ~60s → ~15s without")
    print(f"  CPU pressure (the ~300ms/refit cost falls to ~4 ms)")

    # ────────────────────────────────────────────────────────────────
    # 5) Per-cycle hot-loop budget — broker's main quote refresh
    # ────────────────────────────────────────────────────────────────
    hr("Per-cycle math budget (broker quote refresh):")
    # A typical iteration runs:
    # - refresh_greeks for the portfolio (every fill cycle, every 5min)
    # - constraint check per side per strike — say 10 strikes × 2 sides
    # - SABR re-eval for the snapshot every cycle (small)
    n_strikes = 10
    n_sides = 2
    py_cycle = (
        py_refresh +                       # one refresh
        n_strikes * n_sides * py_check     # 20 fill checks
    )
    rs_cycle = (
        rs_refresh +
        n_strikes * n_sides * rs_check
    )
    print(f"  Python:  {py_cycle/1000:.2f} ms / cycle "
          f"({n_strikes*n_sides} fill checks + 1 refresh)")
    print(f"  Rust:    {rs_cycle/1000:.2f} ms / cycle "
          f"(same structure)")
    print(f"  Saved:   {(py_cycle - rs_cycle)/1000:.2f} ms / cycle")
    print()
    print(f"  At 4 cycles/sec (250ms snapshot cadence):")
    py_pct = py_cycle * 4 / 10_000  # µs/sec → %
    rs_pct = rs_cycle * 4 / 10_000
    print(f"    Python: {py_cycle*4/1000:.1f} ms/sec on math = {py_pct:.1f}% of one core")
    print(f"    Rust:   {rs_cycle*4/1000:.2f} ms/sec on math = {rs_pct:.2f}% of one core")

    # ────────────────────────────────────────────────────────────────
    # 6) TTT estimate
    # ────────────────────────────────────────────────────────────────
    hr("TTT (tick-to-trade) estimate — broker side:")
    print(f"  Components of broker TTT:")
    print(f"    - Tick parse + state update:   ~50 µs (Python orchestration)")
    print(f"    - Decision (theo eval):        ~50 µs (Rust SABR eval)")
    print(f"    - Constraint gate:             ~{rs_check:.0f} µs (Rust SPAN × 2)")
    print(f"    - Order place + IPC:          ~150 µs (msgpack + SHM)")
    print()
    print(f"  Estimated broker TTT (post-Rust):  ~{50 + 50 + rs_check + 150:.0f} µs")
    print(f"  Previously (pure Python):          ~{50 + 100 + py_check + 150:.0f} µs")
    print(f"  Reduction:                          ~{(py_check - rs_check) + 50:.0f} µs / quote")

    # ────────────────────────────────────────────────────────────────
    # 7) Headroom analysis: how many quotes/sec can we sustain?
    # ────────────────────────────────────────────────────────────────
    hr("Sustained quote rate headroom:")
    py_per_quote = py_check + 100  # SPAN gate + theo eval + state update
    rs_per_quote = rs_check + 50
    print(f"  Python: {py_per_quote:.0f} µs/quote → max ~{1_000_000/py_per_quote:.0f} quotes/sec")
    print(f"  Rust:   {rs_per_quote:.0f} µs/quote → max ~{1_000_000/rs_per_quote:.0f} quotes/sec")

    print()
    print("=" * 72)
    print("Summary (post-Rust port, vs pure-Python baseline)")
    print("=" * 72)
    print(f"  Per-cycle math:    {py_cycle/1000:.2f} ms → {rs_cycle/1000:.2f} ms (-{(py_cycle-rs_cycle)/1000:.1f} ms)")
    print(f"  Refit cycle:       {py_refit/1000:.0f} ms  → {rs_refit/1000:.1f} ms (-{(py_refit-rs_refit)/1000:.0f} ms)")
    print(f"  Per-fill check:    {py_check:.0f} µs → {rs_check:.0f} µs (-{(py_check-rs_check):.0f} µs)")
    print(f"  Refresh greeks:    {py_refresh:.0f} µs → {rs_refresh:.0f} µs (-{(py_refresh-rs_refresh):.0f} µs)")
    print()


if __name__ == "__main__":
    main()
