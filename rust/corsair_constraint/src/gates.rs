//! Gate outcomes — accept / reject reasons.

#[derive(Debug, Clone, PartialEq)]
pub enum GateOutcome {
    /// Fill passes all gates.
    Accepted,
    /// Fill rejected. `reason` is a short tag suitable for
    /// rejection-counter telemetry; `detail` is the full message.
    Rejected { reason: String, detail: String },
    /// Fill accepted under the margin-escape rule (currently breached
    /// margin; this fill reduces it). Distinguished from `Accepted`
    /// for telemetry visibility.
    AcceptedMarginEscape { detail: String },
}

impl GateOutcome {
    pub fn is_accepted(&self) -> bool {
        matches!(self, GateOutcome::Accepted | GateOutcome::AcceptedMarginEscape { .. })
    }

    pub fn rejection_reason(&self) -> Option<&str> {
        match self {
            GateOutcome::Rejected { reason, .. } => Some(reason),
            _ => None,
        }
    }
}
