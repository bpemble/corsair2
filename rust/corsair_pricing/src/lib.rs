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

// ─────────────────────────────────────────────────────────────────────
// Phase 6 (mm_service_split.md): SABR implied vol + the trader's
// decide_quote on the same crate. Mirrors src/sabr.py:sabr_implied_vol
// and src/trader/quote_decision.py:decide. Parity tests in
// tests/test_pricing_parity.py and tests/test_decision_parity.py.

/// Hagan 2002 SABR implied vol approximation.
///
/// Mirrors `src/sabr.py:sabr_implied_vol`. ATM is detected at
/// |F-K| < 1e-7 * F (matches the Python `eps = 1e-7` branch).
#[pyfunction]
fn sabr_implied_vol(
    f: f64, k: f64, t: f64,
    alpha: f64, beta: f64, rho: f64, nu: f64,
) -> f64 {
    if t <= 0.0 || alpha <= 0.0 || f <= 0.0 || k <= 0.0 {
        return if alpha > 0.0 { alpha } else { 0.01 };
    }
    let eps = 1e-7;

    // ATM
    if (f - k).abs() < eps * f {
        let fmid = f;
        let fmid_beta = fmid.powf(1.0 - beta);
        let term1 = alpha / fmid_beta;
        let p1 = ((1.0 - beta).powi(2) / 24.0) * alpha * alpha
            / fmid.powf(2.0 - 2.0 * beta);
        let p2 = 0.25 * rho * beta * nu * alpha / fmid_beta;
        let p3 = (2.0 - 3.0 * rho * rho) * nu * nu / 24.0;
        return term1 * (1.0 + (p1 + p2 + p3) * t);
    }

    // General case
    let fk = f * k;
    let fk_beta = fk.powf((1.0 - beta) / 2.0);
    let log_fk = (f / k).ln();

    let z = (nu / alpha) * fk_beta * log_fk;
    let xz = if z.abs() < eps {
        1.0
    } else {
        let sqrt_term = (1.0 - 2.0 * rho * z + z * z).sqrt();
        z / ((sqrt_term + z - rho) / (1.0 - rho)).ln()
    };

    let one_minus_beta = 1.0 - beta;
    let denom1 = fk_beta
        * (1.0
            + one_minus_beta.powi(2) / 24.0 * log_fk.powi(2)
            + one_minus_beta.powi(4) / 1920.0 * log_fk.powi(4));
    let p1 = one_minus_beta.powi(2) / 24.0 * alpha * alpha
        / fk.powf(one_minus_beta);
    let p2 = 0.25 * rho * beta * nu * alpha / fk_beta;
    let p3 = (2.0 - 3.0 * rho * rho) * nu * nu / 24.0;

    (alpha / denom1) * xz * (1.0 + (p1 + p2 + p3) * t)
}

/// Trader's quote decision (Option B per mm_service_split). Returns a
/// dict mirroring src/trader/quote_decision.py:decide.
///
/// Args mirror the Python signature; vol_params is a Python dict with
/// either SABR keys (alpha, beta, rho, nu) or SVI keys (a, b, rho, m, sigma).
/// Only SABR is wired today — SVI returns a "skip" with reason
/// "model_not_in_rust" and the caller (Python) handles SVI in pure
/// Python until the SVI port lands.
#[pyfunction]
#[pyo3(signature = (forward, strike, expiry, right, side, vol_params,
                    market_bid, market_ask, min_edge_ticks, tick_size, tte))]
fn decide_quote(
    py: Python<'_>,
    forward: f64,
    strike: f64,
    expiry: &str,
    right: &str,
    side: &str,
    vol_params: &Bound<'_, PyAny>,
    market_bid: Option<f64>,
    market_ask: Option<f64>,
    min_edge_ticks: i32,
    tick_size: f64,
    tte: f64,
) -> PyResult<PyObject> {
    let dict = pyo3::types::PyDict::new_bound(py);
    dict.set_item("side", side)?;
    dict.set_item("strike", strike)?;
    dict.set_item("expiry", expiry)?;
    dict.set_item("right", right)?;
    dict.set_item("price", py.None())?;
    dict.set_item("theo", py.None())?;
    dict.set_item("iv", py.None())?;

    if vol_params.is_none() {
        dict.set_item("action", "skip")?;
        dict.set_item("reason", "no_vol_surface")?;
        return Ok(dict.into());
    }

    // Extract model field; only "sabr" handled in Rust for v1.
    let model: String = vol_params
        .get_item("model")
        .ok()
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();
    let iv = if model == "sabr" {
        let alpha: f64 = vol_params.get_item("alpha")?.extract()?;
        let beta: f64 = vol_params.get_item("beta")?.extract()?;
        let p_rho: f64 = vol_params.get_item("rho")?.extract()?;
        let nu: f64 = vol_params.get_item("nu")?.extract()?;
        sabr_implied_vol(forward, strike, tte, alpha, beta, p_rho, nu)
    } else {
        // SVI or unknown — punt back to Python.
        dict.set_item("action", "skip")?;
        dict.set_item("reason", "model_not_in_rust")?;
        return Ok(dict.into());
    };

    if iv <= 0.0 || iv.is_nan() {
        dict.set_item("action", "skip")?;
        dict.set_item("reason", "iv_invalid")?;
        return Ok(dict.into());
    }
    let theo = black76_price(forward, strike, tte, iv, 0.0, right);
    if theo <= 0.0 {
        dict.set_item("action", "skip")?;
        dict.set_item("reason", "theo_unavailable")?;
        return Ok(dict.into());
    }
    dict.set_item("iv", iv)?;
    dict.set_item("theo", theo)?;

    let edge = (min_edge_ticks as f64) * tick_size;

    let result = match side {
        "BUY" => {
            let target = theo - edge;
            if target <= 0.0 {
                ("skip", "target_nonpositive", None::<f64>)
            } else if let Some(ask) = market_ask {
                if ask > 0.0 && target >= ask {
                    ("skip", "would_cross_ask", Some(target))
                } else {
                    let q = (target / tick_size).round() * tick_size;
                    ("place", "edge_below_theo", Some(q))
                }
            } else {
                let q = (target / tick_size).round() * tick_size;
                ("place", "edge_below_theo", Some(q))
            }
        }
        "SELL" => {
            let target = theo + edge;
            if let Some(bid) = market_bid {
                if bid > 0.0 && target <= bid {
                    ("skip", "would_cross_bid", Some(target))
                } else {
                    let q = (target / tick_size).round() * tick_size;
                    ("place", "edge_above_theo", Some(q))
                }
            } else {
                let q = (target / tick_size).round() * tick_size;
                ("place", "edge_above_theo", Some(q))
            }
        }
        _ => ("skip", "unknown_side", None::<f64>),
    };
    dict.set_item("action", result.0)?;
    dict.set_item("reason", result.1)?;
    if let Some(p) = result.2 {
        dict.set_item("price", p)?;
    }
    Ok(dict.into())
}

#[pymodule]
fn corsair_pricing(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(black76_price, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol, m)?)?;
    m.add_function(wrap_pyfunction!(sabr_implied_vol, m)?)?;
    m.add_function(wrap_pyfunction!(decide_quote, m)?)?;
    Ok(())
}
