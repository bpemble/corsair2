//! Fill ingestion outcomes.

/// Result of applying a fill to the position book.
#[derive(Debug, Clone, PartialEq)]
pub enum FillOutcome {
    /// Fill opened a new position.
    Opened,
    /// Fill increased the size of an existing same-side position.
    Increased,
    /// Fill partially closed a position; some quantity remains.
    PartiallyClosed,
    /// Fill closed the entire position; it was removed from the book.
    Closed,
    /// Fill flipped a position to the opposite side (e.g., closed
    /// long 2, then opened short 1 from a sell-3).
    Flipped,
    /// Fill rejected: product not registered, or zero quantity.
    Rejected(String),
}
