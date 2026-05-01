//! Pure-Rust pricing primitives for the trader. Mirrors the math in
//! `corsair_pricing` (the PyO3 crate) but with no Python deps. Single
//! source of truth for the formulas is `src/sabr.py`.
//!
//! For the binary's hot path we want native Rust calls (no FFI cost).
//! The corsair_pricing crate stays as the Python extension.

use statrs::distribution::{ContinuousCDF, Normal};

/// Black-76 option price. Mirrors corsair_pricing::black76_price.
pub fn black76_price(f: f64, k: f64, t: f64, sigma: f64, r: f64, right: char) -> f64 {
    let is_call = right == 'C' || right == 'c';
    if t <= 0.0 || sigma <= 0.0 || f <= 0.0 || k <= 0.0 {
        return if is_call { (f - k).max(0.0) } else { (k - f).max(0.0) };
    }
    let sqrt_t = t.sqrt();
    let d1 = ((f / k).ln() + 0.5 * sigma * sigma * t) / (sigma * sqrt_t);
    let d2 = d1 - sigma * sqrt_t;
    let n = Normal::new(0.0, 1.0).unwrap();
    let disc = (-r * t).exp();
    if is_call {
        disc * (f * n.cdf(d1) - k * n.cdf(d2))
    } else {
        disc * (k * n.cdf(-d2) - f * n.cdf(-d1))
    }
}

/// SVI raw total variance. Mirrors svi_total_variance in src/sabr.py.
#[inline(always)]
fn svi_total_variance(k: f64, a: f64, b: f64, rho: f64, m: f64, sigma: f64) -> f64 {
    let dk = k - m;
    a + b * (rho * dk + (dk * dk + sigma * sigma).sqrt())
}

/// SVI implied vol from log-moneyness. Mirrors svi_implied_vol in
/// src/sabr.py (and the recently-ported corsair_pricing version).
///
/// IMPORTANT: caller must pass the FIT-TIME forward, not current spot.
/// SVI's `m` is anchored on log(K/F_fit). See trader/quote_decision.py
/// docstring for the 2026-05-01 incident that motivated this rule.
pub fn svi_implied_vol(
    f: f64, k_strike: f64, t: f64,
    a: f64, b: f64, rho: f64, m: f64, sigma: f64,
) -> f64 {
    if t <= 0.0 || k_strike <= 0.0 || f <= 0.0 {
        return 0.0;
    }
    let k = (k_strike / f).ln();
    let w = svi_total_variance(k, a, b, rho, m, sigma);
    if w <= 0.0 {
        return 0.001;
    }
    (w / t).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn black76_intrinsic_at_zero_vol() {
        // T=0 collapses to intrinsic.
        assert_eq!(black76_price(100.0, 90.0, 0.0, 0.2, 0.0, 'C'), 10.0);
        assert_eq!(black76_price(100.0, 110.0, 0.0, 0.2, 0.0, 'P'), 10.0);
    }

    #[test]
    fn svi_intel_check() {
        // Reproduces the 2026-05-01 fit-forward test:
        // F_fit=6.021, K=5.6, T=0.07
        // svi_total_variance with these params should give ~0.00484
        let f = 6.021;
        let k = 5.6;
        let t = 25.5 / 365.0;
        let iv = svi_implied_vol(
            f, k, t,
            0.0019008499098876505,
            0.03656021179212421,
            -0.7899231280970652,
            -0.08124400811300346,
            0.07679654333384238,
        );
        // Should be ~0.253
        assert!((iv - 0.253).abs() < 0.005, "iv={}", iv);
    }
}
