//! Per-resting-order composite key.

use chrono::NaiveDate;
use corsair_broker_api::{Right, Side};
use serde::{Deserialize, Serialize};

/// Composite key for one resting quote: (strike, expiry, right, side).
/// We may have at most one BUY and one SELL resting per (strike,
/// expiry, right) combination — that's our quoting model.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct OurKey {
    pub strike_key: i64,
    pub expiry: NaiveDate,
    pub right: Right,
    pub side: Side,
}

impl OurKey {
    pub fn new(strike: f64, expiry: NaiveDate, right: Right, side: Side) -> Self {
        Self {
            strike_key: (strike * 10_000.0).round() as i64,
            expiry,
            right,
            side,
        }
    }

    pub fn strike(&self) -> f64 {
        self.strike_key as f64 / 10_000.0
    }
}
