// Rust hot-path math for Corsair v2.
//
// Mirrors src/pricing.py (PricingEngine.black76_price + .implied_vol).
// The Python module remains the source of truth for behavior; this
// crate must produce numerically-equivalent output. tests/test_pricing_parity.py
// asserts equivalence to ~1e-9 (Black-76 price) / ~1e-5 (implied vol) across
// 1000s of random inputs.
//
// Brent's method is hand-rolled to match scipy.optimize.brentq's
// classical algorithm (combination of bisection + secant + inverse
// quadratic interpolation, with the standard mflag five-condition
// fallback to bisection).

use pyo3::prelude::*;
use statrs::distribution::{ContinuousCDF, Normal};

#[inline]
fn norm_cdf(x: f64) -> f64 {
    // Normal::new(0,1) is cheap (just stores mean+stddev). Constructing
    // per-call avoids the lazy-static / once_cell dance.
    Normal::new(0.0, 1.0).unwrap().cdf(x)
}

#[pyfunction]
#[pyo3(signature = (f, k, t, sigma, r=0.0, right="C"))]
fn black76_price(f: f64, k: f64, t: f64, sigma: f64, r: f64, right: &str) -> f64 {
    let is_call = right.eq_ignore_ascii_case("C");
    if t <= 0.0 || sigma <= 0.0 || f <= 0.0 || k <= 0.0 {
        return if is_call {
            (f - k).max(0.0)
        } else {
            (k - f).max(0.0)
        };
    }
    let sqrt_t = t.sqrt();
    let d1 = ((f / k).ln() + 0.5 * sigma * sigma * t) / (sigma * sqrt_t);
    let d2 = d1 - sigma * sqrt_t;
    let df = (-r * t).exp();
    if is_call {
        df * (f * norm_cdf(d1) - k * norm_cdf(d2))
    } else {
        df * (k * norm_cdf(-d2) - f * norm_cdf(-d1))
    }
}

#[pyfunction]
#[pyo3(signature = (market_price, f, k, t, r=0.0, right="C"))]
fn implied_vol(market_price: f64, f: f64, k: f64, t: f64, r: f64, right: &str) -> Option<f64> {
    if t <= 0.0 || market_price <= 0.0 {
        return None;
    }
    let is_call = right.eq_ignore_ascii_case("C");
    let intrinsic = if is_call {
        (f - k).max(0.0)
    } else {
        (k - f).max(0.0)
    };
    let df = (-r * t).exp();
    if market_price < df * intrinsic - 1e-10 {
        return None;
    }
    let objective = |sigma: f64| black76_price(f, k, t, sigma, r, right) - market_price;
    brent(&objective, 0.01, 5.0, 1e-6, 100)
}

/// Brent's method for univariate root-finding. Mirrors
/// `scipy.optimize.brentq(f, a, b, xtol=tol, maxiter=max_iter)`.
///
/// Returns None if the bracket [a, b] does not contain a root
/// (i.e., f(a) and f(b) have the same sign), matching scipy's
/// behavior of raising ValueError in the same case (which the
/// Python wrapper catches and converts to None).
fn brent<F: Fn(f64) -> f64>(f: &F, mut a: f64, mut b: f64, xtol: f64, max_iter: usize) -> Option<f64> {
    let mut fa = f(a);
    let mut fb = f(b);
    if fa * fb >= 0.0 {
        return None;
    }
    if fa.abs() < fb.abs() {
        std::mem::swap(&mut a, &mut b);
        std::mem::swap(&mut fa, &mut fb);
    }
    let mut c = a;
    let mut fc = fa;
    let mut d = a; // initial value unused — first iteration is mflag=true so the !mflag branch can't reach d
    let mut mflag = true;

    for _ in 0..max_iter {
        if (b - a).abs() < xtol {
            return Some(b);
        }
        if fb == 0.0 {
            return Some(b);
        }

        let s_iqi_or_secant = if fa != fc && fb != fc {
            // Inverse quadratic interpolation.
            a * fb * fc / ((fa - fb) * (fa - fc))
                + b * fa * fc / ((fb - fa) * (fb - fc))
                + c * fa * fb / ((fc - fa) * (fc - fb))
        } else {
            // Secant fallback when f-values would cause divide-by-zero in IQI.
            b - fb * (b - a) / (fb - fa)
        };

        // Five-condition fallback to bisection (Brent's classical safeguard).
        let lo = (3.0 * a + b) / 4.0;
        let hi = b;
        let between = if lo <= hi {
            s_iqi_or_secant >= lo && s_iqi_or_secant <= hi
        } else {
            s_iqi_or_secant >= hi && s_iqi_or_secant <= lo
        };
        let cond1 = !between;
        let cond2 = mflag && (s_iqi_or_secant - b).abs() >= (b - c).abs() / 2.0;
        let cond3 = !mflag && (s_iqi_or_secant - b).abs() >= (c - d).abs() / 2.0;
        let cond4 = mflag && (b - c).abs() < xtol;
        let cond5 = !mflag && (c - d).abs() < xtol;
        let s = if cond1 || cond2 || cond3 || cond4 || cond5 {
            mflag = true;
            (a + b) / 2.0
        } else {
            mflag = false;
            s_iqi_or_secant
        };

        let fs = f(s);
        d = c;
        c = b;
        fc = fb;
        if fa * fs < 0.0 {
            b = s;
            fb = fs;
        } else {
            a = s;
            fa = fs;
        }
        if fa.abs() < fb.abs() {
            std::mem::swap(&mut a, &mut b);
            std::mem::swap(&mut fa, &mut fb);
        }
    }
    Some(b)
}

#[pymodule]
fn corsair_pricing(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(black76_price, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol, m)?)?;
    Ok(())
}
