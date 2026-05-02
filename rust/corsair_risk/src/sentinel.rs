//! Induced-kill sentinel files (CLAUDE.md §9 + risk_monitor.py).
//!
//! Each entry maps a switch name (cli arg) to the triple
//! (filename, kill_type, kill_source). Operators write the sentinel
//! file via `scripts/induce_kill_switch.py`; the risk monitor checks
//! for it on every cycle and fires the matching kill through the
//! real path. Used for v1.4 §9.4 Gate 0 verification.

use crate::kill::{KillSource, KillType};
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct InducedSentinel {
    pub key: &'static str,
    pub filename: &'static str,
    pub kill_type: KillType,
    /// The source we're inducing — wrapped in `KillSource::Induced(inner)`
    /// when the kill fires.
    pub inner_source: KillSource,
}

/// Static table of induced sentinels. Mirrors `INDUCE_SENTINELS` in
/// `src/risk_monitor.py`.
pub static INDUCED_SENTINELS: &[InducedSentinel] = &[
    InducedSentinel {
        key: "daily_pnl",
        filename: "corsair_induce_daily_pnl",
        kill_type: KillType::Halt,
        inner_source: KillSource::DailyHalt,
    },
    InducedSentinel {
        key: "margin",
        filename: "corsair_induce_margin",
        kill_type: KillType::Halt,
        inner_source: KillSource::Risk,
    },
    InducedSentinel {
        key: "delta",
        filename: "corsair_induce_delta",
        kill_type: KillType::HedgeFlat,
        inner_source: KillSource::Risk,
    },
    InducedSentinel {
        key: "theta",
        filename: "corsair_induce_theta",
        kill_type: KillType::Halt,
        inner_source: KillSource::Risk,
    },
    InducedSentinel {
        key: "vega",
        filename: "corsair_induce_vega",
        kill_type: KillType::Halt,
        inner_source: KillSource::Risk,
    },
];

/// Resolve the sentinel directory. Mirrors the Python convention:
/// `CORSAIR_INDUCE_DIR` env var, defaulting to `/tmp`.
pub fn sentinel_dir() -> PathBuf {
    std::env::var("CORSAIR_INDUCE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_sentinels_have_unique_keys() {
        let mut keys: Vec<_> = INDUCED_SENTINELS.iter().map(|s| s.key).collect();
        keys.sort();
        keys.dedup();
        assert_eq!(keys.len(), INDUCED_SENTINELS.len());
    }

    #[test]
    fn daily_pnl_sentinel_has_daily_halt_source() {
        let s = INDUCED_SENTINELS.iter().find(|s| s.key == "daily_pnl").unwrap();
        assert_eq!(s.inner_source, KillSource::DailyHalt);
    }

    #[test]
    fn delta_sentinel_uses_hedge_flat() {
        let s = INDUCED_SENTINELS.iter().find(|s| s.key == "delta").unwrap();
        assert_eq!(s.kill_type, KillType::HedgeFlat);
    }
}
