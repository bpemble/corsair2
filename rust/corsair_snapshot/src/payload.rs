//! Snapshot JSON payload — what the dashboard reads.

use chrono::NaiveDate;
use corsair_broker_api::Right;
use serde::{Deserialize, Serialize};

/// Top-level snapshot. Stable wire format; CHANGES to this struct
/// must coordinate with the Streamlit dashboard.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Snapshot {
    pub schema_version: u32,
    pub timestamp_ns: u64,
    pub portfolio: PortfolioSnapshot,
    pub kill: KillSnapshot,
    pub hedges: Vec<HedgeSnapshot>,
    /// Account-level values from broker (NLV, margin, BP, etc.).
    pub account: AccountSnapshot,
    /// Underlying prices per product.
    pub underlying: std::collections::HashMap<String, f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioSnapshot {
    pub net_delta: f64,
    pub net_theta: f64,
    pub net_vega: f64,
    pub net_gamma: f64,
    pub long_count: u32,
    pub short_count: u32,
    pub gross_positions: u32,
    pub fills_today: u32,
    pub realized_pnl_persisted: f64,
    pub mtm_pnl: f64,
    pub daily_pnl: f64,
    pub spread_capture_today: f64,
    pub session_open_nlv: Option<f64>,
    pub positions: Vec<PositionSnapshot>,
    /// Per-product breakdown.
    pub per_product: std::collections::HashMap<String, PortfolioPerProduct>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioPerProduct {
    pub net_delta: f64,
    pub net_theta: f64,
    pub net_vega: f64,
    pub long_count: u32,
    pub short_count: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PositionSnapshot {
    pub product: String,
    pub strike: f64,
    pub expiry: NaiveDate,
    pub right: Right,
    pub quantity: i32,
    pub avg_fill_price: f64,
    pub current_price: f64,
    pub delta: f64,
    pub gamma: f64,
    pub theta: f64,
    pub vega: f64,
    pub multiplier: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KillSnapshot {
    pub killed: bool,
    pub source: Option<String>,
    pub kill_type: Option<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HedgeSnapshot {
    pub product: String,
    pub hedge_qty: i32,
    pub avg_entry_f: f64,
    pub realized_pnl_usd: f64,
    pub mtm_usd: f64,
    pub mode: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AccountSnapshot {
    pub net_liquidation: f64,
    pub maintenance_margin: f64,
    pub initial_margin: f64,
    pub buying_power: f64,
    pub realized_pnl_today: f64,
    /// IBKR scale factor (synthetic SPAN ÷ IBKR MaintMargin).
    /// 1.0 means no scaling applied yet.
    pub ibkr_scale: f64,
}

pub const SCHEMA_VERSION: u32 = 1;
