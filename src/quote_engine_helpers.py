"""Quote-engine helper utilities and small classes.

Extracted from quote_engine.py to reduce that file's footprint and keep
the well-bounded pieces (token bucket, fill burst tracker, expiry
resolution, side gating) testable and reasonable to read.

Design rules for what lives here:
  - No reference to QuoteManager or its internal state
  - No ib_insync imports (these helpers run independent of the broker)
  - Standalone enough to be unit-tested without mocks

quote_engine.py re-exports everything from this module so existing
imports (`from src.quote_engine import TokenBucket, FillBurstTracker, ...`)
continue to work without churn.
"""

import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


OrderKey = tuple[float, str, str, str]  # (strike, expiry, right, side)
ORDER_REF_PREFIX = "corsair"

# Stage 0 burst-injection sentinel (Thread 3 deployment runbook Phase 1).
# Path inside the corsair container; analogous to risk_monitor's
# INDUCE_SENTINEL_DIR convention. Operators write this via
# scripts/induce_burst.py to inject synthetic fills into the burst tracker
# without touching the IBKR event path.
BURST_INJECT_SENTINEL_DIR = os.environ.get("CORSAIR_INDUCE_DIR", "/tmp")
BURST_INJECT_SENTINEL = "corsair_inject_burst"

# Orders auto-cancel after this many seconds if not refreshed. The engine's
# last-resort deadman — bounds how long a quote can sit with stale data if
# a crash, container kill, network outage, or gateway hang slips past every
# other recovery layer. Set to 30s as the equilibrium between API churn
# (refresh frequency) and stale-quote risk; the watchdog handles common-case
# hang detection at ~20s so the GTD only needs to be a backstop. The spec
# nominally calls for "~60s" but we deliberately tightened after building
# the watchdog (the spec was drafted before that discussion).
GTD_EXPIRY_SECONDS = 30
# Re-send the order to refresh GTD when less than this many seconds remain.
# With 30s expiry and 10s threshold, an unchanged order is refreshed every ~20s.
GTD_REFRESH_THRESHOLD_SECONDS = 10


class TokenBucket:
    """Simple monotonic-clock token bucket for outbound API rate limiting.

    Capacity = max burst, refill = sustained rate (tokens/sec). `try_consume`
    refills lazily on each call (no background task), returns True if a
    token was available and consumed, False if the bucket was empty.

    Single-asyncio-loop only — no thread safety. That matches our runtime
    (ib_insync runs everything on one event loop).

    Ported from corsair v1's order_manager.py rate limiter. Used to keep us
    under IBKR's documented ~50 msg/sec API throttle.
    """

    __slots__ = ("_capacity", "_refill", "_tokens", "_last_ns", "_drops")

    def __init__(self, capacity: float, refill_per_sec: float):
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last_ns = time.monotonic_ns()
        self._drops = 0

    def try_consume(self, n: float = 1.0) -> bool:
        now_ns = time.monotonic_ns()
        elapsed = (now_ns - self._last_ns) / 1e9
        self._last_ns = now_ns
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        if self._tokens >= n:
            self._tokens -= n
            return True
        self._drops += 1
        return False

    @property
    def drops(self) -> int:
        return self._drops

    @property
    def tokens(self) -> float:
        return self._tokens


class FillBurstTracker:
    """Thread 3 Layer C rolling fill-burst window.

    Maintains a deque of (ts_ns, side) for fills landed within the
    trailing ``window_sec``. ``record_and_evaluate`` appends the new
    fill, evicts everything older than the window, and returns the
    same-side and any-side counts (inclusive of the just-added fill).

    Side semantics: caller passes ``"BUY"`` or ``"SELL"`` per fill;
    same-side counts match strict equality. Mixed C/P fills on the same
    side count together — that matches the brief's "trigger_C1 = side ==
    this_side, on any strike" definition (cross-strike same-side burst
    is the canonical P1 signature).

    O(1) amortized on the hot path: ``_evict`` trims the deque head each
    call against ``now_ns − window_ns``; total work is bounded by
    lifetime fill count.
    """

    __slots__ = ("_window_ns", "_events")

    def __init__(self, window_sec: float):
        if window_sec <= 0:
            raise ValueError(f"window_sec must be > 0, got {window_sec}")
        self._window_ns = int(window_sec * 1_000_000_000)
        self._events: deque = deque()

    def _evict(self, now_ns: int) -> None:
        cutoff = now_ns - self._window_ns
        ev = self._events
        while ev and ev[0][0] <= cutoff:
            ev.popleft()

    def record_and_evaluate(self, ts_ns: int, side: str
                            ) -> tuple[int, int]:
        """Append one fill, evict stale entries, return
        ``(same_side_count, any_side_count)`` over the trailing window
        INCLUDING the fill just appended."""
        self._events.append((ts_ns, side))
        self._evict(ts_ns)
        same = sum(1 for _, s in self._events if s == side)
        return same, len(self._events)

    def count_in_window(self, now_ns: int) -> int:
        """Read-only any-side count over the trailing window. Used to
        emit ``burst_1s_at_fill`` after the fill has been recorded.
        Caller is responsible for ordering: query AFTER record_and_evaluate
        so the just-added fill is included."""
        self._evict(now_ns)
        return len(self._events)


def resolve_enabled_expiries(tokens, subscribed: list) -> list:
    """Resolve config.quoting.enabled_expiries tokens to concrete YYYYMMDD
    strings using the live subscribed expiry list (front-first, sorted).

    Accepted token formats:
      - "front"       → subscribed[0]
      - "front+N"     → subscribed[N] (if present)
      - "back1"..     → subscribed[1]
      - explicit "YYYYMMDD" → passed through if present in subscribed

    Tokens that don't resolve are logged and skipped. Returns the resolved
    list in the order given. Deduped preserving order.
    """
    if not subscribed:
        return []
    out: list = []
    seen: set = set()
    for tok in tokens or []:
        resolved = None
        t = str(tok).strip().lower()
        if t == "front":
            resolved = subscribed[0]
        elif t.startswith("front+"):
            try:
                idx = int(t.split("+", 1)[1])
                if 0 <= idx < len(subscribed):
                    resolved = subscribed[idx]
            except ValueError:
                pass
        elif len(t) == 8 and t.isdigit():
            if t.upper() in subscribed:
                resolved = t.upper()
            else:
                # original casing
                if str(tok) in subscribed:
                    resolved = str(tok)
        if resolved is None:
            logger.warning(
                "enabled_expiries: token %r did not resolve against subscribed=%s",
                tok, subscribed,
            )
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _gtd_string(seconds_from_now: int = GTD_EXPIRY_SECONDS) -> str:
    """Return an IB-formatted goodTillDate string N seconds in the future."""
    dt = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_from_now)
    return dt.strftime("%Y%m%d %H:%M:%S") + " UTC"


def should_quote_side(
    option, side: str,
    constraint_checker, sabr, config,
) -> tuple[bool, str]:
    """Check all constraints and filters for a potential quote.

    Returns (should_quote, rejection_reason).
    """
    # Constraint check (margin + delta + theta)
    passes, reason = constraint_checker.check_constraints(option, side)
    if not passes:
        return False, reason

    # Stale-quote filter (theo buffer is enforced at price construction)
    if config.pricing.sabr_enabled and sabr.last_calibration is not None:
        if sabr.is_quote_stale(option):
            return False, "stale_quote"

    return True, "ok"
