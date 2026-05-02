//! Position and account state types.

use serde::{Deserialize, Serialize};

use crate::contract::Contract;

/// A position held in the account.
#[derive(Debug, Clone)]
pub struct Position {
    pub contract: Contract,
    /// Signed quantity. Positive = long, negative = short, zero = flat
    /// (positions of 0 are typically not returned by gateways but we
    /// allow it here for completeness).
    pub quantity: i32,
    /// Volume-weighted average entry price (basis).
    pub avg_cost: f64,
    /// Realized P&L for closed legs of this position (today, in account
    /// currency). Some gateways don't expose this; adapters return 0.0
    /// when unavailable.
    pub realized_pnl: f64,
    /// Unrealized P&L marked at the gateway's most recent quote.
    /// Adapters update this best-effort; the runtime computes its own
    /// MTM from option prices for accuracy.
    pub unrealized_pnl: f64,
}

/// Snapshot of account-level state. Returned by
/// `Broker::account_values`. Cached in the adapter; freshness depends
/// on the gateway's update cadence (~5 min for IBKR account values,
/// real-time for FCM/iLink).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountSnapshot {
    /// NLV (net liquidation value). The "what's the account worth"
    /// number. Used as the denominator for cap-percentage gates.
    pub net_liquidation: f64,
    /// SPAN-equivalent maintenance margin requirement at the gateway.
    /// IBKR's `MaintMarginReq`; calibrates synthetic SPAN's IBKR scale
    /// factor (CLAUDE.md §3).
    pub maintenance_margin: f64,
    /// Initial margin requirement (typically larger than maint).
    pub initial_margin: f64,
    /// Available capital for new opens.
    pub buying_power: f64,
    /// Realized P&L so far today.
    pub realized_pnl_today: f64,
    pub timestamp_ns: u64,
}
