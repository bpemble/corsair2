//! Send-or-update decision logic. Mirrors `_send_or_update` in
//! `src/quote_engine.py` and the Rust trader's similar logic.

use serde::{Deserialize, Serialize};

use crate::book::OrderBook;
use crate::key::OurKey;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SendOrUpdateConfig {
    /// Tick size for the product (e.g. HG = 0.0005).
    pub tick_size: f64,
    /// Minimum price delta in TICKS that triggers a re-place vs the
    /// resting order. < dead_band → skip.
    pub dead_band_ticks: i32,
    /// GTD lifetime in seconds (typical 30s).
    pub gtd_lifetime_s: f64,
    /// Refresh order this many seconds before GTD expiry. Set to
    /// e.g. 26.5s when gtd_lifetime is 30s — overrides dead-band.
    pub gtd_refresh_lead_s: f64,
    /// Hard floor between sends per key (defensive backstop).
    pub min_send_interval_ms: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum OrderAction {
    /// Place a new order at this key + price + qty. The runtime
    /// issues `Broker::place_order` then calls
    /// `OrderBook::upsert_pending` with the same key.
    Place {
        key: OurKey,
        price: f64,
        qty: u32,
    },
    /// Cancel the existing order then place a new one. The runtime
    /// issues both broker calls (cancel first, then place).
    CancelAndReplace {
        key: OurKey,
        price: f64,
        qty: u32,
    },
    /// Cancel without replacing. Used when the new evaluated price
    /// is invalid or the side is no longer wanted.
    Cancel { key: OurKey },
    /// No action — within dead-band or cooldown. Counter increments.
    SkipDeadBand { key: OurKey },
    SkipCooldown { key: OurKey },
}

/// Inputs to one decide_send call. `now_ns` is monotonic; runtime
/// passes `time::Instant::now()` converted.
pub struct DecideContext {
    pub key: OurKey,
    pub new_price: f64,
    pub new_qty: u32,
    pub now_ns: u64,
}

/// Decide whether to place / replace / cancel / skip given the
/// current book state and config.
///
/// Three rules in order:
/// 1. **Cooldown**: per-key min_send_interval_ms hard floor.
/// 2. **GTD-refresh**: when age > (gtd_lifetime - gtd_refresh_lead),
///    bypass dead-band and re-place.
/// 3. **Dead-band**: when |new - rest| < dead_band_ticks, skip.
pub fn decide_send(
    cfg: &SendOrUpdateConfig,
    book: &OrderBook,
    ctx: DecideContext,
) -> OrderAction {
    let dead_band = cfg.dead_band_ticks as f64 * cfg.tick_size;
    let cooldown_ns = (cfg.min_send_interval_ms as u64) * 1_000_000;

    let existing = book.get(&ctx.key);
    if let Some(rec) = existing {
        // Cooldown floor: even if we want to place, refuse if too soon.
        if rec.last_send_ns > 0 && ctx.now_ns.saturating_sub(rec.last_send_ns) < cooldown_ns {
            return OrderAction::SkipCooldown { key: ctx.key };
        }
        let age_s = (ctx.now_ns.saturating_sub(rec.last_send_ns) as f64) / 1e9;
        let needs_refresh = age_s >= (cfg.gtd_lifetime_s - cfg.gtd_refresh_lead_s);
        let price_diff = (ctx.new_price - rec.price).abs();
        let in_band = price_diff < dead_band;

        if !needs_refresh && in_band {
            return OrderAction::SkipDeadBand { key: ctx.key };
        }
        // Re-placement required (price moved enough OR GTD refresh).
        return OrderAction::CancelAndReplace {
            key: ctx.key,
            price: ctx.new_price,
            qty: ctx.new_qty,
        };
    }

    // No existing order — place fresh.
    OrderAction::Place {
        key: ctx.key,
        price: ctx.new_price,
        qty: ctx.new_qty,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::book::OrderBook;
    use chrono::NaiveDate;
    use corsair_broker_api::{Right, Side};

    fn cfg() -> SendOrUpdateConfig {
        SendOrUpdateConfig {
            tick_size: 0.0005,
            dead_band_ticks: 1,
            gtd_lifetime_s: 30.0,
            gtd_refresh_lead_s: 3.5,
            min_send_interval_ms: 250,
        }
    }

    fn key() -> OurKey {
        OurKey::new(
            6.05,
            NaiveDate::from_ymd_opt(2026, 6, 26).unwrap(),
            Right::Call,
            Side::Buy,
        )
    }

    #[test]
    fn no_existing_order_places() {
        let book = OrderBook::new();
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.025,
                new_qty: 1,
                now_ns: 1_000_000_000,
            },
        );
        matches!(action, OrderAction::Place { .. });
    }

    #[test]
    fn within_dead_band_skips() {
        let mut book = OrderBook::new();
        book.upsert_pending(key(), 0.025, 1, 1_000_000_000);
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.0252, // 0.4 ticks of 0.0005
                new_qty: 1,
                now_ns: 5_000_000_000, // 4s later — well under refresh
            },
        );
        match action {
            OrderAction::SkipDeadBand { .. } => {}
            other => panic!("expected SkipDeadBand, got {other:?}"),
        }
    }

    #[test]
    fn outside_dead_band_replaces() {
        let mut book = OrderBook::new();
        book.upsert_pending(key(), 0.025, 1, 1_000_000_000);
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.030, // 10 ticks
                new_qty: 1,
                now_ns: 5_000_000_000,
            },
        );
        match action {
            OrderAction::CancelAndReplace { price, .. } => assert_eq!(price, 0.030),
            other => panic!("expected CancelAndReplace, got {other:?}"),
        }
    }

    #[test]
    fn gtd_refresh_overrides_dead_band() {
        let mut book = OrderBook::new();
        book.upsert_pending(key(), 0.025, 1, 1_000_000_000);
        // 27s later — past gtd_refresh_lead from gtd_lifetime
        // (30 - 3.5 = 26.5s threshold).
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.0252, // would be in dead-band
                new_qty: 1,
                now_ns: 28_000_000_000,
            },
        );
        match action {
            OrderAction::CancelAndReplace { .. } => {}
            other => panic!("expected refresh CancelAndReplace, got {other:?}"),
        }
    }

    #[test]
    fn cooldown_floor_blocks_rapid_resend() {
        let mut book = OrderBook::new();
        book.upsert_pending(key(), 0.025, 1, 1_000_000_000);
        // 100ms later — under 250ms cooldown.
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.030, // would normally trigger replace
                new_qty: 1,
                now_ns: 1_100_000_000,
            },
        );
        match action {
            OrderAction::SkipCooldown { .. } => {}
            other => panic!("expected SkipCooldown, got {other:?}"),
        }
    }

    #[test]
    fn after_cooldown_replace_allowed() {
        let mut book = OrderBook::new();
        book.upsert_pending(key(), 0.025, 1, 1_000_000_000);
        let action = decide_send(
            &cfg(),
            &book,
            DecideContext {
                key: key(),
                new_price: 0.030,
                new_qty: 1,
                now_ns: 1_500_000_000, // 500ms later
            },
        );
        matches!(action, OrderAction::CancelAndReplace { .. });
    }
}
