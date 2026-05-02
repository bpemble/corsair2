//! Aggregated portfolio Greeks across positions.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Per-product or aggregate Greek totals. Used by risk gates,
/// constraint checks, and snapshot output.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PortfolioGreeks {
    /// Sum of position deltas (multiplier-applied where appropriate;
    /// per-contract delta scaled to "contract-equivalent" units).
    pub net_delta: f64,
    /// Sum of position thetas in $/day, multiplier-applied.
    pub net_theta: f64,
    /// Sum of position vegas in $/1% vol, multiplier-applied.
    pub net_vega: f64,
    /// Sum of position gammas in $-denominated terms.
    pub net_gamma: f64,
    /// Total number of LONG positions (qty > 0).
    pub long_count: u32,
    /// Total number of SHORT positions (qty < 0).
    pub short_count: u32,
    /// Total absolute position count.
    pub gross_positions: u32,
}

impl PortfolioGreeks {
    pub fn add(&mut self, qty: i32, delta: f64, theta: f64, vega: f64, gamma: f64) {
        let q = qty as f64;
        self.net_delta += delta * q;
        self.net_theta += theta * q;
        self.net_vega += vega * q;
        self.net_gamma += gamma * q;
        if qty > 0 {
            self.long_count += 1;
        } else if qty < 0 {
            self.short_count += 1;
        }
        if qty != 0 {
            self.gross_positions += 1;
        }
    }
}

/// Aggregate result returned by [`PortfolioState::aggregate`].
/// Provides both per-product breakdown and the cross-product totals.
/// The risk gates that pick "worst across products" iterate
/// `per_product`; the daily snapshot can use either.
#[derive(Debug, Clone, Default)]
pub struct AggregateResult {
    pub total: PortfolioGreeks,
    pub per_product: HashMap<String, PortfolioGreeks>,
}
