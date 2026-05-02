//! `PortfolioState` — aggregate position book.
//!
//! Maps to today's `src/position_manager.py::PortfolioState` but with
//! v3 architecture: the position book consumes a [`MarketView`] trait
//! for greek refresh, doesn't directly own market data or vol surface
//! state.

use chrono::{DateTime, NaiveDate, Utc};
use corsair_broker_api::Right;
use corsair_pricing::greeks::compute_greeks;
use std::collections::HashMap;

use crate::aggregation::{AggregateResult, PortfolioGreeks};
use crate::fill::FillOutcome;
use crate::market_view::{strike_key as strike_key_fn, MarketView};
use crate::position::{Position, PositionKey, ProductRegistry};

/// Aggregate portfolio state.
///
/// Owns:
/// - Vec<Position> (ordered by insertion)
/// - HashMap<PositionKey, usize> (index for fast lookup)
/// - daily accounting (fills_today, spread_capture, daily_pnl,
///   realized_pnl_persisted, session_open_nlv)
/// - ProductRegistry
///
/// Does NOT own market data, vol surfaces, kill switches, or order
/// lifecycle — those are in their own crates.
pub struct PortfolioState {
    positions: Vec<Position>,
    /// Index for O(1) lookup on add_fill. Values are indices into
    /// `positions`. Rebuilt after position removal to maintain
    /// consistency.
    index: HashMap<PositionKey, usize>,
    registry: ProductRegistry,

    // Daily accounting
    pub fills_today: u32,
    pub spread_capture_today: f64,
    pub spread_capture_mid_today: f64,
    pub daily_pnl: f64,
    pub realized_pnl_persisted: f64,
    pub session_open_nlv: Option<f64>,
}

impl PortfolioState {
    pub fn new(registry: ProductRegistry) -> Self {
        Self {
            positions: Vec::new(),
            index: HashMap::new(),
            registry,
            fills_today: 0,
            spread_capture_today: 0.0,
            spread_capture_mid_today: 0.0,
            daily_pnl: 0.0,
            realized_pnl_persisted: 0.0,
            session_open_nlv: None,
        }
    }

    /// Construct an empty `PortfolioState` with no products. Mostly
    /// for tests; production should always register at least one.
    pub fn empty() -> Self {
        Self::new(ProductRegistry::new())
    }

    pub fn registry(&self) -> &ProductRegistry {
        &self.registry
    }

    pub fn registry_mut(&mut self) -> &mut ProductRegistry {
        &mut self.registry
    }

    pub fn positions(&self) -> &[Position] {
        &self.positions
    }

    pub fn position_count(&self) -> usize {
        self.positions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.positions.is_empty()
    }

    fn make_key(product: &str, strike: f64, expiry: NaiveDate, right: Right) -> PositionKey {
        PositionKey {
            product: product.to_string(),
            strike_key: strike_key_fn(strike),
            expiry,
            right,
        }
    }

    /// Replace the entire position book. Used after `seed_from_broker`
    /// reconciliation. Preserves daily accounting fields.
    pub fn replace_positions(&mut self, positions: Vec<Position>) {
        self.positions = positions;
        self.rebuild_index();
    }

    fn rebuild_index(&mut self) {
        self.index.clear();
        for (i, p) in self.positions.iter().enumerate() {
            let key = Self::make_key(&p.product, p.strike, p.expiry, p.right);
            self.index.insert(key, i);
        }
    }

    /// Apply a fill to the book. Returns the outcome describing what
    /// happened (for the caller's logging / risk-callback decisions).
    ///
    /// Mirrors `position_manager.add_fill`. The avg_fill_price update
    /// rule is the position's mark — Python's behavior is to set
    /// avg_fill_price to the FILL price (not a volume-weighted
    /// average across previous fills); we preserve that here for
    /// parity. Volume-weighted averaging happens in IBKR's own
    /// position book (avgCost from `IB.positions()`), which is what
    /// reconciliation re-syncs against.
    pub fn add_fill(
        &mut self,
        product: &str,
        strike: f64,
        expiry: NaiveDate,
        right: Right,
        quantity: i32,
        fill_price: f64,
        spread_captured: f64,
        spread_captured_mid: f64,
    ) -> FillOutcome {
        if quantity == 0 {
            return FillOutcome::Rejected("zero quantity".into());
        }

        let multiplier = match self.registry.multiplier_for(product) {
            Some(m) => m,
            None => {
                log::warn!(
                    "add_fill: product {} not registered; defaulting multiplier to 100",
                    product
                );
                100.0
            }
        };

        let key = Self::make_key(product, strike, expiry, right);
        if let Some(&idx) = self.index.get(&key) {
            // Existing position. Merge. Read fields up front so we
            // don't hold a mutable borrow across record_fill_accounting.
            let prev_qty = self.positions[idx].quantity;
            let new_qty = prev_qty + quantity;

            if new_qty == 0 {
                self.positions.remove(idx);
                self.rebuild_index();
                self.record_fill_accounting(quantity, spread_captured, spread_captured_mid);
                return FillOutcome::Closed;
            }
            // Mutate fields, drop the borrow, then update accounting.
            {
                let pos = &mut self.positions[idx];
                pos.quantity = new_qty;
                pos.avg_fill_price = fill_price;
                pos.fill_time = Utc::now();
            }
            self.record_fill_accounting(quantity, spread_captured, spread_captured_mid);

            if (prev_qty > 0 && new_qty > 0 && new_qty > prev_qty)
                || (prev_qty < 0 && new_qty < 0 && new_qty < prev_qty)
            {
                FillOutcome::Increased
            } else if prev_qty.signum() != new_qty.signum() && prev_qty.signum() != 0 {
                FillOutcome::Flipped
            } else {
                FillOutcome::PartiallyClosed
            }
        } else {
            // New position.
            let pos = Position {
                product: product.to_string(),
                strike,
                expiry,
                right,
                quantity,
                avg_fill_price: fill_price,
                fill_time: Utc::now(),
                multiplier,
                delta: 0.0,
                gamma: 0.0,
                theta: 0.0,
                vega: 0.0,
                current_price: 0.0,
            };
            self.positions.push(pos);
            self.index.insert(key, self.positions.len() - 1);
            self.record_fill_accounting(quantity, spread_captured, spread_captured_mid);
            FillOutcome::Opened
        }
    }

    fn record_fill_accounting(
        &mut self,
        quantity: i32,
        spread_captured: f64,
        spread_captured_mid: f64,
    ) {
        self.fills_today += quantity.unsigned_abs();
        self.spread_capture_today += spread_captured;
        self.spread_capture_mid_today += spread_captured_mid;
    }

    /// Recompute Greeks for every position using the supplied
    /// [`MarketView`]. Skips positions whose product has no
    /// underlying yet, or whose IV resolution returns None — those
    /// positions retain their previous Greeks until next refresh.
    pub fn refresh_greeks(&mut self, market: &dyn MarketView) {
        for pos in self.positions.iter_mut() {
            let f = match market.underlying_price(&pos.product) {
                Some(v) if v > 0.0 => v,
                _ => continue,
            };
            let iv = market
                .iv_for(&pos.product, pos.strike, pos.expiry, pos.right)
                .unwrap_or_else(|| {
                    self.registry
                        .get(&pos.product)
                        .map(|p| p.default_iv)
                        .unwrap_or(0.30)
                });
            let tte = time_to_expiry_years(pos.expiry);
            if tte <= 0.0 {
                continue;
            }
            let g = compute_greeks(f, pos.strike, tte, iv, 0.0, pos.right.as_char(), pos.multiplier);
            pos.delta = g.delta;
            pos.gamma = g.gamma;
            pos.theta = g.theta;
            pos.vega = g.vega;
            if let Some(p) = market.current_price(&pos.product, pos.strike, pos.expiry, pos.right)
            {
                if p > 0.0 {
                    pos.current_price = p;
                }
            }
        }
    }

    /// Aggregate Greeks per-product AND across all products.
    pub fn aggregate(&self) -> AggregateResult {
        let mut total = PortfolioGreeks::default();
        let mut per_product: HashMap<String, PortfolioGreeks> = HashMap::new();
        for p in &self.positions {
            total.add(p.quantity, p.delta, p.theta, p.vega, p.gamma);
            per_product
                .entry(p.product.clone())
                .or_default()
                .add(p.quantity, p.delta, p.theta, p.vega, p.gamma);
        }
        AggregateResult { total, per_product }
    }

    /// Convenience: net delta across all products. Use
    /// `aggregate().per_product[product].net_delta` for per-product.
    pub fn net_delta(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.delta * p.quantity as f64)
            .sum()
    }

    pub fn net_theta(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.theta * p.quantity as f64)
            .sum()
    }

    pub fn net_vega(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.vega * p.quantity as f64)
            .sum()
    }

    pub fn net_gamma(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.gamma * p.quantity as f64)
            .sum()
    }

    pub fn long_count(&self) -> u32 {
        self.positions.iter().filter(|p| p.quantity > 0).count() as u32
    }

    pub fn short_count(&self) -> u32 {
        self.positions.iter().filter(|p| p.quantity < 0).count() as u32
    }

    pub fn gross_positions(&self) -> u32 {
        self.positions.iter().filter(|p| p.quantity != 0).count() as u32
    }

    /// Per-product delta (signed, qty-weighted).
    pub fn delta_for_product(&self, product: &str) -> f64 {
        self.positions
            .iter()
            .filter(|p| p.product == product)
            .map(|p| p.delta * p.quantity as f64)
            .sum()
    }

    pub fn theta_for_product(&self, product: &str) -> f64 {
        self.positions
            .iter()
            .filter(|p| p.product == product)
            .map(|p| p.theta * p.quantity as f64)
            .sum()
    }

    pub fn vega_for_product(&self, product: &str) -> f64 {
        self.positions
            .iter()
            .filter(|p| p.product == product)
            .map(|p| p.vega * p.quantity as f64)
            .sum()
    }

    pub fn positions_for_product(&self, product: &str) -> Vec<&Position> {
        self.positions.iter().filter(|p| p.product == product).collect()
    }

    /// Mark-to-market P&L using the supplied [`MarketView`] for
    /// fresh prices, falling back to each position's cached
    /// `current_price` when no live observation exists.
    ///
    /// MTM = sum_pos(quantity * (current_price - avg_fill_price) * multiplier)
    pub fn compute_mtm_pnl(&self, market: &dyn MarketView) -> f64 {
        let mut total = 0.0;
        for p in &self.positions {
            let live = market
                .current_price(&p.product, p.strike, p.expiry, p.right)
                .unwrap_or(p.current_price);
            if live <= 0.0 {
                continue;
            }
            total += (p.quantity as f64) * (live - p.avg_fill_price) * p.multiplier;
        }
        total
    }

    /// Reset daily counters at session rollover (CME 17:00 CT).
    /// Does NOT touch positions — those persist across sessions.
    /// `realized_pnl_persisted` is also retained until daily_state
    /// is explicitly reset by the caller.
    pub fn reset_daily(&mut self) {
        self.fills_today = 0;
        self.spread_capture_today = 0.0;
        self.spread_capture_mid_today = 0.0;
        self.daily_pnl = 0.0;
    }

    /// Find a position by key (for snapshot / risk inspection).
    pub fn get_position(
        &self,
        product: &str,
        strike: f64,
        expiry: NaiveDate,
        right: Right,
    ) -> Option<&Position> {
        let key = Self::make_key(product, strike, expiry, right);
        self.index.get(&key).map(|&i| &self.positions[i])
    }
}

/// Compute time-to-expiry in years from a date. Uses CME's calendar
/// convention: 365.25 days/year, end-of-day expiry.
pub fn time_to_expiry_years(expiry: NaiveDate) -> f64 {
    let now: DateTime<Utc> = Utc::now();
    let expiry_end = expiry.and_hms_opt(20, 0, 0).unwrap(); // 20:00 UTC ≈ 15:00 CT
    let expiry_dt = DateTime::<Utc>::from_naive_utc_and_offset(expiry_end, Utc);
    let secs = (expiry_dt - now).num_seconds() as f64;
    if secs <= 0.0 {
        0.0
    } else {
        secs / (365.25 * 24.0 * 3600.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::market_view::RecordingMarketView;
    use crate::position::ProductInfo;
    use chrono::NaiveDate;

    fn registry() -> ProductRegistry {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        r
    }

    fn exp_2026_06() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 6, 26).unwrap()
    }

    // ── add_fill ────────────────────────────────────────────────

    #[test]
    fn fill_into_empty_book_opens_position() {
        let mut p = PortfolioState::new(registry());
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        assert_eq!(outcome, FillOutcome::Opened);
        assert_eq!(p.position_count(), 1);
        assert_eq!(p.fills_today, 1);
    }

    #[test]
    fn fill_increasing_long_position() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 2, 0.025, 0.0, 0.0);
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.030, 0.0, 0.0);
        assert_eq!(outcome, FillOutcome::Increased);
        assert_eq!(p.positions()[0].quantity, 3);
    }

    #[test]
    fn opposite_fill_partially_closes() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 3, 0.025, 0.0, 0.0);
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, -1, 0.030, 0.0, 0.0);
        assert_eq!(outcome, FillOutcome::PartiallyClosed);
        assert_eq!(p.positions()[0].quantity, 2);
    }

    #[test]
    fn opposite_fill_closes_completely() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 2, 0.025, 0.0, 0.0);
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, -2, 0.030, 0.0, 0.0);
        assert_eq!(outcome, FillOutcome::Closed);
        assert_eq!(p.position_count(), 0);
    }

    #[test]
    fn over_close_flips_to_short() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, -3, 0.030, 0.0, 0.0);
        assert_eq!(outcome, FillOutcome::Flipped);
        assert_eq!(p.positions()[0].quantity, -2);
    }

    #[test]
    fn zero_quantity_rejected() {
        let mut p = PortfolioState::new(registry());
        let outcome = p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 0, 0.025, 0.0, 0.0);
        matches!(outcome, FillOutcome::Rejected(_));
        assert_eq!(p.position_count(), 0);
    }

    #[test]
    fn unregistered_product_uses_fallback_multiplier() {
        let mut p = PortfolioState::empty();
        p.add_fill("ZZZ", 10.0, exp_2026_06(), Right::Call, 1, 0.5, 0.0, 0.0);
        assert_eq!(p.position_count(), 1);
        assert_eq!(p.positions()[0].multiplier, 100.0);
    }

    // ── Greeks aggregation ────────────────────────────────────

    #[test]
    fn refresh_greeks_with_market_view_populates_position() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        let view = RecordingMarketView::new();
        view.set_underlying("HG", 6.05);
        view.set_iv("HG", 6.05, exp_2026_06(), Right::Call, 0.45);
        view.set_current_price("HG", 6.05, exp_2026_06(), Right::Call, 0.026);
        p.refresh_greeks(&view);
        let pos = &p.positions()[0];
        assert!(pos.delta != 0.0, "delta populated");
        assert!(pos.vega > 0.0, "vega positive");
        assert_eq!(pos.current_price, 0.026);
    }

    #[test]
    fn refresh_skips_positions_with_no_underlying() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        let view = RecordingMarketView::new(); // empty
        p.refresh_greeks(&view);
        assert_eq!(p.positions()[0].delta, 0.0);
    }

    #[test]
    fn aggregate_sums_across_products() {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        r.register(ProductInfo {
            product: "ETHUSDRR".into(),
            multiplier: 50.0,
            default_iv: 0.50,
        });
        let mut p = PortfolioState::new(r);
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        p.add_fill("ETHUSDRR", 3000.0, exp_2026_06(), Right::Put, -2, 50.0, 0.0, 0.0);
        let agg = p.aggregate();
        assert_eq!(agg.total.long_count, 1);
        assert_eq!(agg.total.short_count, 1);
        assert_eq!(agg.per_product.len(), 2);
    }

    #[test]
    fn delta_for_product_isolates_correctly() {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        r.register(ProductInfo {
            product: "ETH".into(),
            multiplier: 50.0,
            default_iv: 0.40,
        });
        let mut p = PortfolioState::new(r);
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        p.add_fill("ETH", 3000.0, exp_2026_06(), Right::Call, 2, 50.0, 0.0, 0.0);
        let view = RecordingMarketView::new();
        view.set_underlying("HG", 6.05);
        view.set_underlying("ETH", 3000.0);
        view.set_iv("HG", 6.05, exp_2026_06(), Right::Call, 0.40);
        view.set_iv("ETH", 3000.0, exp_2026_06(), Right::Call, 0.60);
        p.refresh_greeks(&view);
        let dh = p.delta_for_product("HG");
        let de = p.delta_for_product("ETH");
        assert!(dh > 0.0 && de > 0.0);
        // HG and ETH should not contaminate each other.
        assert_ne!(dh, de);
    }

    // ── MTM ────────────────────────────────────────────────────

    #[test]
    fn mtm_uses_live_market_price() {
        let mut p = PortfolioState::new(registry());
        // Long 1 call @ 0.025; current 0.030; multiplier 25000.
        // MTM = (0.030 - 0.025) * 1 * 25000 = $125
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        let view = RecordingMarketView::new();
        view.set_current_price("HG", 6.05, exp_2026_06(), Right::Call, 0.030);
        let mtm = p.compute_mtm_pnl(&view);
        assert!((mtm - 125.0).abs() < 1e-6, "got {mtm}");
    }

    #[test]
    fn mtm_zero_when_no_price_and_no_cached() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 0.0, 0.0);
        let view = RecordingMarketView::new(); // no current price
        let mtm = p.compute_mtm_pnl(&view);
        assert_eq!(mtm, 0.0);
    }

    // ── Daily reset ───────────────────────────────────────────

    #[test]
    fn reset_daily_clears_counters_keeps_positions() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 1, 0.025, 1.5, 1.0);
        assert!(p.fills_today > 0);
        p.reset_daily();
        assert_eq!(p.fills_today, 0);
        assert_eq!(p.spread_capture_today, 0.0);
        assert_eq!(p.position_count(), 1, "positions persist across day reset");
    }

    // ── Index integrity ───────────────────────────────────────

    #[test]
    fn lookup_after_close_returns_none() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 2, 0.025, 0.0, 0.0);
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, -2, 0.030, 0.0, 0.0);
        assert!(p
            .get_position("HG", 6.05, exp_2026_06(), Right::Call)
            .is_none());
    }

    #[test]
    fn lookup_finds_existing_position() {
        let mut p = PortfolioState::new(registry());
        p.add_fill("HG", 6.05, exp_2026_06(), Right::Call, 2, 0.025, 0.0, 0.0);
        let pos = p
            .get_position("HG", 6.05, exp_2026_06(), Right::Call)
            .unwrap();
        assert_eq!(pos.quantity, 2);
    }

    #[test]
    fn replace_positions_rebuilds_index() {
        let mut p = PortfolioState::new(registry());
        let pos = Position {
            product: "HG".into(),
            strike: 6.05,
            expiry: exp_2026_06(),
            right: Right::Call,
            quantity: 5,
            avg_fill_price: 0.025,
            fill_time: Utc::now(),
            multiplier: 25_000.0,
            delta: 0.0,
            gamma: 0.0,
            theta: 0.0,
            vega: 0.0,
            current_price: 0.0,
        };
        p.replace_positions(vec![pos]);
        assert_eq!(p.position_count(), 1);
        let found = p.get_position("HG", 6.05, exp_2026_06(), Right::Call);
        assert!(found.is_some());
    }
}
