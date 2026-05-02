//! Pre-trade constraint validation.
//!
//! Mirrors `src/constraint_checker.py::ConstraintChecker`. Gates
//! every fill against margin / delta / theta caps. The margin math
//! is delegated to `corsair_pricing::span` (already Rust-fast); this
//! crate adds the orchestration:
//!   - per-product caps (don't mix HG and ETH delta/theta)
//!   - effective-delta gating (options + hedge per CLAUDE.md §14)
//!   - improving-fill exception (allow fills that reduce a breach)
//!   - tier-1 margin escape (CLAUDE.md §7 / §8)
//!   - hard kill limits as final backstop
//!
//! IBKR margin scaling (the 50% over-estimate calibration in
//! synthetic_span.py) is the runtime's responsibility, NOT this
//! crate. The gateway adapter exposes `account_values()` from which
//! the runtime derives the scale factor; this crate consumes the
//! pre-scaled margin values.

pub mod checker;
pub mod gates;

pub use checker::{ConstraintCheck, ConstraintChecker, ConstraintConfig};
pub use gates::GateOutcome;
