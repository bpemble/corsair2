//! Per-option tick state.

use chrono::NaiveDate;
use corsair_broker_api::{InstrumentId, Right};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionTick {
    pub instrument_id: Option<InstrumentId>,
    pub strike: f64,
    pub expiry: NaiveDate,
    pub right: Right,
    pub bid: f64,
    pub ask: f64,
    pub bid_size: u64,
    pub ask_size: u64,
    pub last: f64,
    /// Implied vol from brentq solve on mid; updated on bid/ask change.
    pub iv: f64,
    pub last_updated_ns: u64,
}

impl OptionTick {
    pub fn new(strike: f64, expiry: NaiveDate, right: Right) -> Self {
        Self {
            instrument_id: None,
            strike,
            expiry,
            right,
            bid: 0.0,
            ask: 0.0,
            bid_size: 0,
            ask_size: 0,
            last: 0.0,
            iv: 0.0,
            last_updated_ns: 0,
        }
    }

    /// Mid price; None if either side is missing.
    pub fn mid(&self) -> Option<f64> {
        if self.bid > 0.0 && self.ask > 0.0 && self.bid < self.ask {
            Some((self.bid + self.ask) / 2.0)
        } else {
            None
        }
    }

    /// Best available current price for MTM:
    /// mid → last → 0.
    pub fn current_price(&self) -> f64 {
        self.mid().unwrap_or(if self.last > 0.0 { self.last } else { 0.0 })
    }
}
