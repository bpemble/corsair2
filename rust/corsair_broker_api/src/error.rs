//! Broker error types. Categorized so consumers can branch on kind
//! without parsing message strings.

use thiserror::Error;

/// All errors a Broker can surface. Categorized so:
/// - Connection-related → watchdog flips disconnect kill (auto-clears
///   on reconnect)
/// - Order-related → OMS routes to the originating order's lifecycle
/// - Rate-limit → backoff at the OMS
/// - Internal → log + alert; usually means adapter bug
#[derive(Debug, Clone, Error)]
pub enum BrokerError {
    /// Lost the connection (TCP reset, gateway crash, network blip).
    /// IBKR Error 1100 / disconnect events / FIX session disconnect.
    #[error("connection lost: {0}")]
    ConnectionLost(String),

    /// We tried to operate while disconnected. Usually a bug in the
    /// runtime; adapter shouldn't reject if the disconnect is
    /// transient (it'll buffer or wait).
    #[error("not connected: {0}")]
    NotConnected(String),

    /// Gateway rejected the order. `reason` carries the
    /// gateway-specific reject string (IBKR error code + message,
    /// FIX OrdRejReason). The OMS surfaces this on the
    /// OrderStatusUpdate stream.
    #[error("order rejected (id={order_id:?}): {reason}")]
    OrderRejected {
        order_id: Option<u64>,
        reason: String,
    },

    /// Tried to cancel an order that's already terminal.
    #[error("cannot cancel terminal order: {0}")]
    CancelTerminal(String),

    /// Tried to modify an order that's already terminal.
    #[error("cannot modify terminal order: {0}")]
    ModifyTerminal(String),

    /// `qualify_*` couldn't find a matching contract.
    #[error("contract not found: {0}")]
    ContractNotFound(String),

    /// Rate limit hit. IBKR's pacing violation, FIX flow control, etc.
    /// Adapter SHOULD back off and retry, not propagate this. If you
    /// see it at the trait surface, the adapter exhausted its retries.
    #[error("rate limited: {0}")]
    RateLimited(String),

    /// Account / permissions error (wrong account, no permission to
    /// trade this product, no funds, etc.). NOT auto-recoverable.
    #[error("account error: {0}")]
    Account(String),

    /// Caller passed an invalid request (negative qty, missing price
    /// on a Limit order, GTD without expiry, etc.).
    #[error("invalid request: {0}")]
    InvalidRequest(String),

    /// Gateway-specific protocol error (IBKR Error 320 / 10197,
    /// FIX session reject). Adapter typically reports these and
    /// continues; persistent occurrences are concerning.
    #[error("protocol error (code={code:?}): {message}")]
    Protocol {
        code: Option<i32>,
        message: String,
    },

    /// Catch-all for adapter internal errors. Means a bug — log,
    /// alert, investigate.
    #[error("internal error: {0}")]
    Internal(String),
}

impl BrokerError {
    /// True if the error is connection-related — caller should expect
    /// the connection stream to also report this and not double-alert.
    pub fn is_connection_error(&self) -> bool {
        matches!(
            self,
            BrokerError::ConnectionLost(_) | BrokerError::NotConnected(_)
        )
    }

    /// True if the caller can safely retry the same operation after
    /// a brief delay.
    pub fn is_retriable(&self) -> bool {
        matches!(
            self,
            BrokerError::RateLimited(_)
                | BrokerError::ConnectionLost(_)
                | BrokerError::Protocol { .. }
        )
    }
}
