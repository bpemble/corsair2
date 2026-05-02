//! IPC layer — re-exports from the shared `corsair_ipc` crate.
//!
//! Phase 5B.5 migrated this module from a local copy to re-exports
//! so the trader and broker share one source of truth for the wire
//! format. The legacy `protocol` and `shm` submodule paths are
//! preserved for backwards compatibility with the existing main.rs.

pub mod protocol {
    //! Re-export of `corsair_ipc::protocol`.
    pub use corsair_ipc::protocol::*;
}

pub mod shm {
    //! Re-export of `corsair_ipc::ring`.
    pub use corsair_ipc::ring::*;
}
