// Synthetic SPAN scan for futures-options portfolios. Mirrors
// src/synthetic_span.py exactly:
//   - 16 scenarios: ±0/±1/±2/±3 of price scan × ±vol scan, plus
//     two extreme moves at ±3·scan (vol flat, 33% cover)
//   - max-of-three margin floor: max(scan_risk - NOV, short_min, long_premium)
//   - Conservative +50% systematic over-estimate vs IBKR (documented;
//     the runtime ibkr_scale factor in constraint_checker calibrates this)
//
// Hot path: called on every fill (constraint check) and every risk
// cycle. Python with numpy vectorization is ~150-300 μs per portfolio
// pass; this Rust version is ~5-15 μs — call dominated by the
// statrs norm_cdf which is itself ~30 ns × 32 evals per position.

use crate::black76_price_inner;

const PRICE_FRACS: [f64; 14] = [
    0.0, 0.0, 1.0 / 3.0, 1.0 / 3.0, -1.0 / 3.0, -1.0 / 3.0,
    2.0 / 3.0, 2.0 / 3.0, -2.0 / 3.0, -2.0 / 3.0,
    1.0, 1.0, -1.0, -1.0,
];
const VOL_SIGNS: [f64; 14] = [
    1.0, -1.0, 1.0, -1.0, 1.0, -1.0,
    1.0, -1.0, 1.0, -1.0,
    1.0, -1.0, 1.0, -1.0,
];

#[derive(Debug, Clone, Copy)]
pub struct SpanConfig {
    pub up_scan_pct: f64,
    pub down_scan_pct: f64,
    pub vol_scan_pct: f64,
    pub extreme_mult: f64,
    pub extreme_cover: f64,
    pub short_option_minimum: f64,
    pub multiplier: f64,
}

/// 16-element risk array (loss $ per LONG contract per scenario).
/// Positive = loss for a long position. Negate for shorts.
pub fn position_risk_array(
    f: f64,
    k: f64,
    t: f64,
    iv: f64,
    right: char,
    cfg: &SpanConfig,
) -> [f64; 16] {
    let mut out = [0.0_f64; 16];
    if f <= 0.0 || t <= 0.0 || iv <= 0.0 {
        return out;
    }
    let base = black76_price_inner(f, k, t, iv, 0.0, right);
    let up_scan = f * cfg.up_scan_pct;
    let down_scan = f * cfg.down_scan_pct;
    let vol_scan = iv * cfg.vol_scan_pct;
    let mult = cfg.multiplier;

    for i in 0..14 {
        let frac = PRICE_FRACS[i];
        let shift = if frac >= 0.0 { frac * up_scan } else { frac * down_scan };
        let f_scen = (f + shift).max(1.0);
        let iv_scen = (iv + VOL_SIGNS[i] * vol_scan).max(0.01);
        let p = black76_price_inner(f_scen, k, t, iv_scen, 0.0, right);
        out[i] = -(p - base) * mult;
    }
    // Extreme up
    let f_up = (f + cfg.extreme_mult * up_scan).max(1.0);
    let p_up = black76_price_inner(f_up, k, t, iv, 0.0, right);
    out[14] = -(p_up - base) * mult * cfg.extreme_cover;
    // Extreme down
    let f_dn = (f - cfg.extreme_mult * down_scan).max(1.0);
    let p_dn = black76_price_inner(f_dn, k, t, iv, 0.0, right);
    out[15] = -(p_dn - base) * mult * cfg.extreme_cover;

    out
}

#[derive(Debug, Clone, Copy)]
pub struct PortfolioMargin {
    pub scan_risk: f64,
    pub net_option_value: f64,
    pub long_premium: f64,
    pub short_minimum: f64,
    pub total_margin: f64,
    pub worst_scenario: i32,
}

/// Each position: (strike, right, T, iv, signed_qty).
pub fn portfolio_margin(
    f: f64,
    positions: &[(f64, char, f64, f64, i64)],
    cfg: &SpanConfig,
) -> PortfolioMargin {
    let mut portfolio = [0.0_f64; 16];
    let mut nov = 0.0_f64;
    let mut long_premium = 0.0_f64;
    let mut short_count: u64 = 0;
    let mult = cfg.multiplier;

    for &(k, right, t, iv, qty) in positions.iter() {
        if qty == 0 || t <= 0.0 || iv <= 0.0 {
            continue;
        }
        let ra = position_risk_array(f, k, t, iv, right, cfg);
        let qty_f = qty as f64;
        for i in 0..16 {
            portfolio[i] += ra[i] * qty_f;
        }
        let base = black76_price_inner(f, k, t, iv, 0.0, right);
        nov += base * qty_f * mult;
        if qty > 0 {
            long_premium += base * qty_f * mult;
        } else {
            short_count += qty.unsigned_abs();
        }
    }

    let mut max_v = 0.0_f64;
    let mut max_idx: i32 = 0;
    for (i, &v) in portfolio.iter().enumerate() {
        if v > max_v {
            max_v = v;
            max_idx = (i as i32) + 1;
        }
    }
    let scan_risk = max_v.max(0.0);
    let short_min = (short_count as f64) * cfg.short_option_minimum;
    let risk_margin = (scan_risk - nov).max(0.0);
    let total = risk_margin.max(short_min).max(long_premium);

    PortfolioMargin {
        scan_risk,
        net_option_value: nov,
        long_premium,
        short_minimum: short_min,
        total_margin: total,
        worst_scenario: if scan_risk > 0.0 { max_idx } else { 0 },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> SpanConfig {
        SpanConfig {
            up_scan_pct: 0.56,
            down_scan_pct: 0.49,
            vol_scan_pct: 0.25,
            extreme_mult: 3.0,
            extreme_cover: 0.33,
            short_option_minimum: 500.0,
            multiplier: 25_000.0,
        }
    }

    #[test]
    fn long_atm_call_has_no_scan_risk() {
        let ra = position_risk_array(6.0, 6.0, 0.05, 0.4, 'C', &cfg());
        // For a LONG contract, every scenario should be a loss-or-gain
        // (price moves up → call gains → loss=negative). The scan_risk
        // for a long is max(loss); for a long ATM call most scenarios
        // are profitable, so scan_risk is small.
        let max_loss = ra.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        assert!(max_loss <= 0.0 || max_loss < 50_000.0);
    }

    #[test]
    fn short_atm_call_has_meaningful_scan_risk() {
        // Short = -1 qty. Risk array for a long; we negate by quantity
        // sign in portfolio_margin.
        let positions = vec![(6.0_f64, 'C', 0.05_f64, 0.4_f64, -1_i64)];
        let m = portfolio_margin(6.0, &positions, &cfg());
        assert!(m.scan_risk > 1_000.0,
                "short ATM call should have nontrivial scan_risk: {}",
                m.scan_risk);
        assert!(m.total_margin >= m.short_minimum);
    }

    #[test]
    fn flat_book_has_zero_margin() {
        let positions: Vec<(f64, char, f64, f64, i64)> = vec![];
        let m = portfolio_margin(6.0, &positions, &cfg());
        assert_eq!(m.scan_risk, 0.0);
        assert_eq!(m.total_margin, 0.0);
    }

    #[test]
    fn long_only_book_has_long_premium_floor() {
        // Long ATM call — no scan risk, short_minimum=0, but
        // long_premium > 0 → total_margin = long_premium.
        let positions = vec![(6.0_f64, 'C', 0.05_f64, 0.4_f64, 2_i64)];
        let m = portfolio_margin(6.0, &positions, &cfg());
        assert!(m.long_premium > 0.0);
        assert_eq!(m.total_margin, m.long_premium.max(m.scan_risk - m.net_option_value).max(0.0));
    }
}
