//! Kill type + source taxonomy + the persistent kill state.

use serde::{Deserialize, Serialize};

/// What action the kill performs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum KillType {
    /// Cancel resting quotes, no flatten. Default for halt-style kills.
    Halt,
    /// Cancel quotes + flatten options + flatten hedge.
    Flatten,
    /// Cancel quotes + force hedge to 0 (no options flatten). Delta kill.
    HedgeFlat,
}

/// Source category — governs auto-clear rules.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum KillSource {
    /// Sticky risk-source kill. Requires manual review + restart.
    Risk,
    /// Cleared by watchdog after reconnect.
    Disconnect,
    /// Auto-clears at next CME session rollover.
    DailyHalt,
    /// Sticky.
    Reconciliation,
    /// Sticky.
    ExceptionStorm,
    /// Sticky (SABR RMSE, latency, abnormal fill rate).
    Operational,
    /// Boot-self-test sentinel kill. Inner carries the underlying
    /// source we're exercising; induced_daily_halt auto-clears at
    /// rollover, others are sticky.
    Induced(Box<KillSource>),
}

impl KillSource {
    /// True if this source auto-clears at session rollover.
    pub fn is_daily_clearable(&self) -> bool {
        matches!(self, KillSource::DailyHalt)
            || matches!(self, KillSource::Induced(inner) if matches!(**inner, KillSource::DailyHalt))
    }

    /// True if this source clears on watchdog reconnect.
    pub fn is_disconnect(&self) -> bool {
        matches!(self, KillSource::Disconnect)
    }

    /// Telemetry-friendly string label.
    pub fn label(&self) -> String {
        match self {
            KillSource::Risk => "risk".into(),
            KillSource::Disconnect => "disconnect".into(),
            KillSource::DailyHalt => "daily_halt".into(),
            KillSource::Reconciliation => "reconciliation".into(),
            KillSource::ExceptionStorm => "exception_storm".into(),
            KillSource::Operational => "operational".into(),
            KillSource::Induced(inner) => format!("induced_{}", inner.label()),
        }
    }
}

/// Active kill state. Constructed when a kill fires; cleared by
/// `clear_disconnect` or `clear_daily_halt` per the source rules.
#[derive(Debug, Clone)]
pub struct KillEvent {
    pub reason: String,
    pub source: KillSource,
    pub kill_type: KillType,
    pub timestamp_ns: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn daily_halt_is_daily_clearable() {
        assert!(KillSource::DailyHalt.is_daily_clearable());
        assert!(!KillSource::Risk.is_daily_clearable());
    }

    #[test]
    fn induced_daily_halt_clearable() {
        let s = KillSource::Induced(Box::new(KillSource::DailyHalt));
        assert!(s.is_daily_clearable());
    }

    #[test]
    fn induced_risk_not_clearable() {
        let s = KillSource::Induced(Box::new(KillSource::Risk));
        assert!(!s.is_daily_clearable());
    }

    #[test]
    fn label_strings_match_python_taxonomy() {
        assert_eq!(KillSource::Risk.label(), "risk");
        assert_eq!(KillSource::DailyHalt.label(), "daily_halt");
        assert_eq!(
            KillSource::Induced(Box::new(KillSource::DailyHalt)).label(),
            "induced_daily_halt"
        );
        assert_eq!(
            KillSource::Induced(Box::new(KillSource::Risk)).label(),
            "induced_risk"
        );
    }
}
