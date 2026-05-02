//! Position struct + product registry.

use chrono::{DateTime, NaiveDate, Utc};
use corsair_broker_api::Right;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Composite key for matching fills to existing positions:
/// (product, strike, expiry, right). Used in [`PortfolioState`]'s
/// internal index to do O(1) lookup on every fill.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PositionKey {
    pub product: String,
    /// Strike price encoded as the integer number of cents (or
    /// other product-defined unit). Avoids f64-as-key issues. Built
    /// from f64 via `PortfolioState::strike_key` (round to 4
    /// decimals — exactly mirrors corsair_trader's strike_key).
    pub strike_key: i64,
    pub expiry: NaiveDate,
    pub right: Right,
}

/// A single option position. Maps 1:1 to today's
/// `src/position_manager.py::Position` but uses Rust-native types and
/// stores the multiplier directly (so MTM math is multi-product
/// safe without a registry lookup at every step).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub product: String,
    pub strike: f64,
    pub expiry: NaiveDate,
    pub right: Right,
    /// Signed: positive = long, negative = short.
    pub quantity: i32,
    /// Volume-weighted average fill price (in points, NOT $).
    /// MTM math: pnl = (current_price - avg_fill_price) * qty * multiplier.
    pub avg_fill_price: f64,
    pub fill_time: DateTime<Utc>,
    pub multiplier: f64,

    // Greeks — populated by [`PortfolioState::refresh_greeks`].
    // Theta is dollar-denominated per calendar day; vega is per
    // 1% vol move, multiplier-applied.
    pub delta: f64,
    pub gamma: f64,
    pub theta: f64,
    pub vega: f64,
    /// Most recent observed mid price (or last). Updated during
    /// `refresh_greeks` from the [`MarketView`].
    pub current_price: f64,
}

impl Position {
    /// Construct the lookup key for this position. Used by
    /// [`PortfolioState`] to merge fills.
    pub fn key(&self, strike_key_fn: impl Fn(f64) -> i64) -> PositionKey {
        PositionKey {
            product: self.product.clone(),
            strike_key: strike_key_fn(self.strike),
            expiry: self.expiry,
            right: self.right,
        }
    }
}

/// Per-product configuration. Drives multiplier resolution in
/// `add_fill` and any product-specific behavior the runtime needs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProductInfo {
    /// IBKR underlying symbol (matches Position.product).
    pub product: String,
    /// Contract multiplier ($/point). HG=25000, ETHUSDRR=50.
    pub multiplier: f64,
    /// Default IV for greek computation when MarketView has no value
    /// yet (typically 30%, matching Python's fallback).
    pub default_iv: f64,
}

/// Lookup table for registered products. Multi-product runtimes
/// register each product before seeding. Defaults to HG-only for
/// the immediate operational scope.
#[derive(Debug, Default, Clone)]
pub struct ProductRegistry {
    products: HashMap<String, ProductInfo>,
}

impl ProductRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, info: ProductInfo) {
        log::info!(
            "ProductRegistry: registering {} (multiplier={})",
            info.product,
            info.multiplier
        );
        self.products.insert(info.product.clone(), info);
    }

    pub fn get(&self, product: &str) -> Option<&ProductInfo> {
        self.products.get(product)
    }

    pub fn products(&self) -> Vec<String> {
        self.products.keys().cloned().collect()
    }

    pub fn multiplier_for(&self, product: &str) -> Option<f64> {
        self.products.get(product).map(|i| i.multiplier)
    }

    pub fn is_empty(&self) -> bool {
        self.products.is_empty()
    }

    pub fn len(&self) -> usize {
        self.products.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_register_and_lookup() {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        assert_eq!(r.multiplier_for("HG"), Some(25_000.0));
        assert_eq!(r.multiplier_for("UNKNOWN"), None);
    }

    #[test]
    fn registry_re_register_overwrites() {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 50_000.0,
            default_iv: 0.40,
        });
        assert_eq!(r.multiplier_for("HG"), Some(50_000.0));
        assert_eq!(r.products().len(), 1);
    }
}
