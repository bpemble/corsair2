//! ATM tracking + strike subscription window.

use serde::{Deserialize, Serialize};

/// Tracks the at-the-money strike and the subscription window
/// around it. Used by `state` to decide when to (un)subscribe to
/// strikes.
#[derive(Debug, Clone, Default)]
pub struct AtmTracker {
    /// Current ATM strike — closest tradeable strike to the
    /// underlying price. Updated when the underlying drifts more
    /// than `recenter_threshold` ticks away.
    pub atm_strike: f64,
    /// Subscription window (low, high) inclusive in strikes.
    pub window: StrikeWindow,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct StrikeWindow {
    pub low: f64,
    pub high: f64,
}

impl AtmTracker {
    pub fn new() -> Self {
        Self::default()
    }

    /// Recenter the window around the new ATM if drift exceeds
    /// `recenter_threshold` ticks. Returns Some(new_window) on
    /// recenter; None otherwise.
    ///
    /// `width_ticks` defines the half-width in strike increments.
    pub fn maybe_recenter(
        &mut self,
        underlying: f64,
        strike_increment: f64,
        width_ticks: i32,
        recenter_threshold_ticks: i32,
    ) -> Option<StrikeWindow> {
        if underlying <= 0.0 || strike_increment <= 0.0 {
            return None;
        }
        let new_atm = (underlying / strike_increment).round() * strike_increment;
        if self.atm_strike == 0.0 {
            // Initial fix.
            self.atm_strike = new_atm;
            let half = (width_ticks as f64) * strike_increment;
            self.window = StrikeWindow {
                low: new_atm - half,
                high: new_atm + half,
            };
            return Some(self.window);
        }
        let drift_ticks = ((new_atm - self.atm_strike) / strike_increment).abs() as i32;
        if drift_ticks < recenter_threshold_ticks {
            return None;
        }
        self.atm_strike = new_atm;
        let half = (width_ticks as f64) * strike_increment;
        self.window = StrikeWindow {
            low: new_atm - half,
            high: new_atm + half,
        };
        Some(self.window)
    }

    pub fn contains(&self, strike: f64) -> bool {
        strike >= self.window.low && strike <= self.window.high
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_observation_initializes_window() {
        let mut atm = AtmTracker::new();
        let w = atm.maybe_recenter(6.05, 0.05, 5, 2);
        assert!(w.is_some());
        let w = w.unwrap();
        assert!((atm.atm_strike - 6.05).abs() < 1e-9);
        assert!((w.low - 5.80).abs() < 1e-9);
        assert!((w.high - 6.30).abs() < 1e-9);
    }

    #[test]
    fn small_drift_no_recenter() {
        let mut atm = AtmTracker::new();
        atm.maybe_recenter(6.05, 0.05, 5, 2);
        let w = atm.maybe_recenter(6.06, 0.05, 5, 2); // 0.2 tick rounded
        assert!(w.is_none(), "shouldn't recenter on sub-tick drift");
    }

    #[test]
    fn large_drift_recenters() {
        let mut atm = AtmTracker::new();
        atm.maybe_recenter(6.00, 0.05, 5, 2);
        let w = atm.maybe_recenter(6.20, 0.05, 5, 2); // 4 ticks of drift
        assert!(w.is_some());
        let w = w.unwrap();
        assert!((w.low - 5.95).abs() < 1e-9);
    }

    #[test]
    fn contains_inclusive_bounds() {
        let mut atm = AtmTracker::new();
        atm.maybe_recenter(6.00, 0.05, 5, 2);
        assert!(atm.contains(6.00));
        assert!(atm.contains(5.75));
        assert!(atm.contains(6.25));
        assert!(!atm.contains(5.74));
        assert!(!atm.contains(6.26));
    }
}
