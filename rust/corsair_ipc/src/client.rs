//! Stub client. The trader currently has its own SHM client at
//! `rust/corsair_trader/src/ipc/shm.rs` (predates this crate).
//! Phase 5B.5 migrates that code here so both sides share one
//! implementation. For now this is a placeholder.

use std::path::PathBuf;

#[allow(dead_code)]
pub struct SHMClient {
    base_path: PathBuf,
}

impl SHMClient {
    pub fn new(base_path: impl Into<PathBuf>) -> Self {
        Self {
            base_path: base_path.into(),
        }
    }
}
