//! Position book + Greek aggregation for the v3 Rust runtime.
//!
//! Maps to today's `src/position_manager.py` but with v3 architecture
//! discipline: the position book does NOT directly couple to market
//! data or vol surfaces. Instead it consumes a [`MarketView`] trait
//! object that the runtime injects (production: corsair_market_data;
//! tests: in-memory stub).
//!
//! # Responsibilities
//!
//! 1. Maintain a vec of [`Position`]s across all registered products
//! 2. Apply broker [`Fill`]s — merging into existing positions or
//!    creating new ones
//! 3. Recompute per-position Greeks from current market data via
//!    [`MarketView`] (calls corsair_pricing::greeks::compute_greeks)
//! 4. Aggregate Greeks per-product and across products
//! 5. Compute MTM P&L using current prices
//! 6. Track daily accounting (fills_today, spread_capture, daily_pnl)
//! 7. Daily reset and session-rollover hooks
//!
//! # Multi-product
//!
//! `product` is the IBKR underlying symbol ("HG", "ETHUSDRR"). Each
//! product has its own multiplier; the registry ([`register_product`])
//! is the single source of truth for that mapping. Positions are
//! aggregated per-product (delta_for_product, etc.) AND across all
//! products (net_delta, etc.). The risk gate that combines them is
//! the responsibility of `corsair_risk` / `corsair_constraint`.
//!
//! # What's NOT here (intentionally)
//!
//! - Market data subscription: that's `corsair_market_data`.
//! - SABR fitting: `corsair_pricing::calibrate` driven by
//!   `corsair_market_data`.
//! - IV resolution policy: implemented in [`MarketView`]
//!   implementations.
//! - Risk gating, kill switches: `corsair_risk`.
//! - Margin computation: `corsair_constraint` (uses corsair_pricing's
//!   SPAN module directly).

pub mod aggregation;
pub mod fill;
pub mod market_view;
pub mod position;
pub mod portfolio;

pub use aggregation::PortfolioGreeks;
pub use fill::FillOutcome;
pub use market_view::{MarketView, NoOpMarketView, RecordingMarketView};
pub use portfolio::PortfolioState;
pub use position::{Position, PositionKey, ProductRegistry, ProductInfo};
