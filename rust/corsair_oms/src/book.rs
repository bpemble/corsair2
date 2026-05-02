//! `OrderBook` — our_orders index keyed by [`OurKey`].

use chrono::{DateTime, Utc};
use corsair_broker_api::{OrderId, OrderStatus};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::key::OurKey;

/// Lifecycle state of one of OUR orders.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderState {
    /// Sent to broker, no place_ack yet.
    Pending,
    /// Live at the exchange (status = Submitted).
    Live,
    /// Cancel sent.
    PendingCancel,
    /// Filled / Cancelled / Rejected — about to be removed.
    Terminal,
}

#[derive(Debug, Clone)]
pub struct OrderRecord {
    pub key: OurKey,
    pub order_id: Option<OrderId>,
    pub price: f64,
    pub qty: u32,
    pub state: OrderState,
    pub placed_at: DateTime<Utc>,
    pub last_send_ns: u64,
}

impl OrderRecord {
    /// Age in seconds since last placement.
    pub fn age_seconds(&self) -> f64 {
        let now: DateTime<Utc> = Utc::now();
        (now - self.placed_at).num_milliseconds() as f64 / 1000.0
    }

    pub fn is_live(&self) -> bool {
        matches!(self.state, OrderState::Live | OrderState::Pending)
    }
}

/// Counters for cycle telemetry. The runtime publishes these in the
/// requote_cycles JSONL stream.
#[derive(Debug, Clone, Default)]
pub struct OrderBookCounters {
    pub places: u32,
    pub cancels: u32,
    pub modifies: u32,
    pub dead_band_skips: u32,
    pub gtd_refreshes: u32,
    pub cooldown_skips: u32,
    pub place_acks: u32,
    pub fills: u32,
    pub rejects: u32,
}

pub struct OrderBook {
    by_key: HashMap<OurKey, OrderRecord>,
    /// Reverse index: order_id → OurKey, for fast resolution from
    /// status updates.
    by_order_id: HashMap<OrderId, OurKey>,
    counters: OrderBookCounters,
}

impl Default for OrderBook {
    fn default() -> Self {
        Self::new()
    }
}

impl OrderBook {
    pub fn new() -> Self {
        Self {
            by_key: HashMap::new(),
            by_order_id: HashMap::new(),
            counters: OrderBookCounters::default(),
        }
    }

    pub fn counters(&self) -> &OrderBookCounters {
        &self.counters
    }

    pub fn counters_mut(&mut self) -> &mut OrderBookCounters {
        &mut self.counters
    }

    pub fn len(&self) -> usize {
        self.by_key.len()
    }

    pub fn is_empty(&self) -> bool {
        self.by_key.is_empty()
    }

    pub fn get(&self, key: &OurKey) -> Option<&OrderRecord> {
        self.by_key.get(key)
    }

    pub fn get_by_order_id(&self, oid: OrderId) -> Option<&OrderRecord> {
        self.by_order_id.get(&oid).and_then(|k| self.by_key.get(k))
    }

    /// Iterate over all live orders.
    pub fn live_orders(&self) -> impl Iterator<Item = &OrderRecord> {
        self.by_key.values().filter(|r| r.is_live())
    }

    /// Insert or replace a record. Used immediately after the
    /// runtime issues a Broker::place_order; the record starts in
    /// state Pending until the place_ack arrives.
    pub fn upsert_pending(
        &mut self,
        key: OurKey,
        price: f64,
        qty: u32,
        last_send_ns: u64,
    ) -> &mut OrderRecord {
        // If we had a previous record for this key, remove its
        // by_order_id entry first (its order may already be
        // cancelled/filled).
        if let Some(prev) = self.by_key.get(&key) {
            if let Some(oid) = prev.order_id {
                self.by_order_id.remove(&oid);
            }
        }
        let rec = OrderRecord {
            key: key.clone(),
            order_id: None,
            price,
            qty,
            state: OrderState::Pending,
            placed_at: Utc::now(),
            last_send_ns,
        };
        self.by_key.insert(key.clone(), rec);
        self.counters.places += 1;
        self.by_key.get_mut(&key).unwrap()
    }

    /// Resolve an order_id from the broker's place_ack and link it
    /// to the pending record at `key`.
    pub fn link_order_id(&mut self, key: &OurKey, oid: OrderId) {
        if let Some(rec) = self.by_key.get_mut(key) {
            rec.order_id = Some(oid);
            self.by_order_id.insert(oid, key.clone());
        }
        self.counters.place_acks += 1;
    }

    /// Apply an order-status update. Updates state and removes the
    /// record if terminal. Returns true if the record existed.
    pub fn apply_status(&mut self, oid: OrderId, status: OrderStatus) -> bool {
        let key = match self.by_order_id.get(&oid).cloned() {
            Some(k) => k,
            None => return false,
        };
        let mut should_remove = false;
        if let Some(rec) = self.by_key.get_mut(&key) {
            match status {
                OrderStatus::PendingSubmit | OrderStatus::PendingCancel => {
                    rec.state = OrderState::Pending;
                }
                OrderStatus::Submitted => {
                    rec.state = OrderState::Live;
                }
                OrderStatus::Filled => {
                    rec.state = OrderState::Terminal;
                    self.counters.fills += 1;
                    should_remove = true;
                }
                OrderStatus::Cancelled => {
                    rec.state = OrderState::Terminal;
                    self.counters.cancels += 1;
                    should_remove = true;
                }
                OrderStatus::Rejected => {
                    rec.state = OrderState::Terminal;
                    self.counters.rejects += 1;
                    should_remove = true;
                }
                OrderStatus::Inactive => {
                    rec.state = OrderState::Terminal;
                    should_remove = true;
                }
            }
        }
        if should_remove {
            self.by_key.remove(&key);
            self.by_order_id.remove(&oid);
        }
        true
    }

    /// Mark a key as PendingCancel after the runtime issued a
    /// Broker::cancel_order. The terminal Cancelled status that
    /// follows will remove it.
    pub fn mark_pending_cancel(&mut self, key: &OurKey) {
        if let Some(rec) = self.by_key.get_mut(key) {
            rec.state = OrderState::PendingCancel;
        }
    }

    /// Snapshot for telemetry — returns a count by state.
    pub fn snapshot_state_counts(&self) -> [(OrderState, u32); 4] {
        let mut counts = [
            (OrderState::Pending, 0u32),
            (OrderState::Live, 0),
            (OrderState::PendingCancel, 0),
            (OrderState::Terminal, 0),
        ];
        for r in self.by_key.values() {
            for slot in counts.iter_mut() {
                if slot.0 == r.state {
                    slot.1 += 1;
                }
            }
        }
        counts
    }

    /// Drop everything. Used by the runtime on a panic_cancel /
    /// cancel_all path AFTER it's issued the broker cancels.
    pub fn clear(&mut self) {
        self.by_key.clear();
        self.by_order_id.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use corsair_broker_api::{Right, Side};

    fn k() -> OurKey {
        OurKey::new(
            6.05,
            NaiveDate::from_ymd_opt(2026, 6, 26).unwrap(),
            Right::Call,
            Side::Buy,
        )
    }

    #[test]
    fn upsert_and_lookup() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        assert!(b.get(&k()).is_some());
        assert_eq!(b.counters().places, 1);
    }

    #[test]
    fn link_order_id_enables_reverse_lookup() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        let rec = b.get_by_order_id(OrderId(42)).unwrap();
        assert_eq!(rec.key, k());
    }

    #[test]
    fn apply_status_submitted_marks_live() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        assert!(b.apply_status(OrderId(42), OrderStatus::Submitted));
        assert_eq!(b.get(&k()).unwrap().state, OrderState::Live);
    }

    #[test]
    fn apply_status_filled_removes_record() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        b.apply_status(OrderId(42), OrderStatus::Filled);
        assert!(b.get(&k()).is_none());
        assert_eq!(b.counters().fills, 1);
    }

    #[test]
    fn apply_status_cancelled_removes_and_counts() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        b.apply_status(OrderId(42), OrderStatus::Cancelled);
        assert!(b.get(&k()).is_none());
        assert_eq!(b.counters().cancels, 1);
    }

    #[test]
    fn apply_status_rejected_increments_rejects() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        b.apply_status(OrderId(42), OrderStatus::Rejected);
        assert_eq!(b.counters().rejects, 1);
    }

    #[test]
    fn upsert_replaces_previous_record_for_key() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        b.upsert_pending(k(), 0.030, 1, 1_000_000_000);
        assert_eq!(b.get(&k()).unwrap().price, 0.030);
        // Old order_id link removed.
        assert!(b.get_by_order_id(OrderId(42)).is_none());
    }

    #[test]
    fn live_orders_filters_terminal() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.link_order_id(&k(), OrderId(42));
        b.apply_status(OrderId(42), OrderStatus::Submitted);
        let count = b.live_orders().count();
        assert_eq!(count, 1);
    }

    #[test]
    fn clear_drops_all() {
        let mut b = OrderBook::new();
        b.upsert_pending(k(), 0.025, 1, 0);
        b.clear();
        assert!(b.is_empty());
    }
}
