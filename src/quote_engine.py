"""Quote engine for Corsair v2.

For each quotable strike:
  - Compute bid/ask: penny-jump incumbent, bounded by theo ± buffer
  - Check that a hypothetical fill passes constraints (margin/delta/theta)
  - Send/update or cancel
"""

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Optional, Tuple

from ib_insync import IB, LimitOrder

from .utils import round_to_tick

LATENCY_RING_SIZE = 500   # rolling window for TTT/RTT/AMEND percentiles
# Soft cap on the in-flight tracking dicts (_pending_rtt, _placed_at_ns,
# _pending_amend, _rtt_captured_oids). When any of these grows past this
# size, we drop the oldest half. Prevents slow leaks when an order goes
# terminal without observation (e.g., GTD-expired before next snapshot).
TRACKING_DICT_MAX = 4_000

# Minimum lifetime before a freshly-placed order is eligible for cancellation.
# IBKR takes ~50-300ms to ack a new order; if we cancel inside that window the
# order goes PendingSubmit → PendingCancel without ever visiting Submitted, so
# the order is invisible on the book and we burn a place/cancel pair. Lowered
# from 750ms to 300ms 2026-04-08 — the canonical_trade fix means we now reliably
# observe the ack, so we don't need the 750ms safety margin we needed when
# orders looked perpetually stuck. 300ms covers the observed amend p50 (~106ms)
# with headroom for the long tail without artificially delaying legitimate
# cancels on real skip conditions.
MIN_ORDER_LIFETIME_MS = 300

logger = logging.getLogger(__name__)

OrderKey = Tuple[float, str, str]  # (strike, right, side)
ORDER_REF_PREFIX = "corsair2"

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


def _gtd_string(seconds_from_now: int = GTD_EXPIRY_SECONDS) -> str:
    """Return an IB-formatted goodTillDate string N seconds in the future."""
    dt = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_from_now)
    return dt.strftime("%Y%m%d %H:%M:%S") + " UTC"


def should_quote_side(
    portfolio, option, side: str, quote_price: float,
    constraint_checker, sabr, config,
) -> Tuple[bool, str]:
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


class QuoteManager:
    """Manages quote lifecycle: send, update, cancel orders on IBKR."""

    def __init__(self, ib: IB, config, market_data, sabr, constraint_checker,
                 csv_logger=None):
        self.ib = ib
        self.config = config
        self.market_data = market_data
        self.sabr = sabr
        self.constraint_checker = constraint_checker
        self.csv_logger = csv_logger
        self.active_orders: Dict[OrderKey, int] = {}  # {(strike, right, side): order_id}
        self._account = config.account.account_id
        self._last_sabr_attempt: Optional[datetime] = None
        self._last_sabr_forward: float = 0.0  # underlying at last calibration
        # Latency rings (microseconds). TTT = tick→placeOrder. RTT = placeOrder→Submitted ack.
        self._ttt_us: Deque[int] = deque(maxlen=LATENCY_RING_SIZE)
        self._rtt_us: Deque[int] = deque(maxlen=LATENCY_RING_SIZE)
        # Amend RTT — separate from _rtt_us because modifies dominate steady
        # state and they have a different latency profile than fresh places
        # (no order-id allocation, no permission validation, just price update).
        self._amend_us: Deque[int] = deque(maxlen=LATENCY_RING_SIZE)
        self._pending_rtt: Dict[int, int] = {}  # order_id -> placeOrder ns
        # Pending amend tracking: (orderId) -> (sent_lmtPrice, sent_ns)
        # Set on every modify path. Cleared when we observe canonical Trade
        # reflect the new price (at which point we record the amend RTT).
        self._pending_amend: Dict[int, Tuple[float, int]] = {}
        # Order placement time per order_id, for fill latency (place→fill).
        # Cleared on fill_handler when the fill arrives. Distinct from
        # _pending_rtt which clears on Submitted ack.
        self._placed_at_ns: Dict[int, int] = {}
        # Most recent tick_received_ns per (strike, right) at decision time.
        self._decision_tick_ns: Dict[Tuple[float, str], int] = {}
        # Consecutive theo_edge skip count per key — used for hysteresis so a
        # single tick of theo_edge violation doesn't drop a resting order
        # that would re-qualify on the next tick. Reset on any non-theo path
        # through _process_side (place or other gate).
        self._theo_edge_streak: Dict[OrderKey, int] = {}
        # orderIds we've already extracted RTT for (so we don't double-count
        # on subsequent snapshot passes).
        self._rtt_captured_oids: set = set()
        # Per-cycle canonical trade index — populated by _build_our_prices_index
        # at the top of each update_quotes / get_active_quotes call. Reading
        # from this dict is O(1) vs an O(N) walk of openTrades, which matters
        # at 4Hz × ~50 active orders. Cleared opportunistically; readers fall
        # back to walking openTrades when the cache is empty (cold path).
        self._canonical_idx: Dict[int, object] = {}

    def _canonical_trade(self, order_id: int):
        """Return the canonical (latest) Trade object for an orderId, or
        None if it's no longer open.

        Hot path: reads from `self._canonical_idx`, which is populated once
        per cycle by `_build_our_prices_index`. The cached entry is the same
        Trade *instance* that ib_insync mutates in place, so reading its
        `.orderStatus.status` always returns the latest value even between
        cache rebuilds.

        Cold path: when the cache is empty (e.g., a caller outside the
        update_quotes / get_active_quotes flow), fall back to walking
        openTrades. This preserves correctness if the call site changes.

        Why we don't cache the placeOrder return value: ib_insync sometimes
        constructs a NEW Trade object when an openOrder callback fires
        (notably after reqAutoOpenOrders adopts the order on clientId=0).
        The Trade returned by placeOrder becomes an orphan that nobody
        updates — it'd stay at PendingSubmit forever. The fix is to always
        re-resolve from the canonical store.

        Subtlety: openTrades() can return MULTIPLE Trade objects with the
        same orderId — the original (stale) Trade and a fresh one from the
        openOrder callback. We MUST return the LAST match in the iteration
        (the canonical one with up-to-date status), not the first.
        """
        cached = self._canonical_idx.get(order_id)
        if cached is not None:
            return cached
        latest = None
        for t in self.ib.openTrades():
            if t.order.orderId == order_id:
                latest = t
        return latest

    def _build_our_prices_index(self) -> Dict[Tuple[float, str], set]:
        """One-pass build of {(strike, right): set(prices)} for self-filter
        use across an entire update_quotes cycle. Walks openTrades exactly
        once per cycle instead of N times (one per quotable strike/right).

        Two-step to handle ib_insync's multi-Trade-per-orderId quirk: first
        build {orderId: canonical_trade} (last-write-wins picks the canonical
        non-orphan instance), then bucket by (strike, right).

        Side effect: populates `self._canonical_idx` so `_canonical_trade()`
        can do O(1) lookups during the rest of the cycle instead of walking
        openTrades on every call.
        """
        canonical: Dict[int, object] = {}
        for t in self.ib.openTrades():
            ref = getattr(t.order, "orderRef", "") or ""
            if ref.startswith(ORDER_REF_PREFIX):
                canonical[t.order.orderId] = t
        self._canonical_idx = canonical  # cache for _canonical_trade reads
        out: Dict[Tuple[float, str], set] = {}
        for t in canonical.values():
            if not self._is_order_live(t):
                continue
            c = t.contract
            if c.symbol != "ETHUSDRR":
                continue
            key = (float(c.strike), c.right)
            out.setdefault(key, set()).add(float(t.order.lmtPrice))
        return out

    def update_quotes(self, portfolio, dirty: Optional[set] = None):
        """Reprice and re-issue quotes.

        If `dirty` is provided, only those (strike, right) pairs are evaluated
        (event-driven path: caller has identified which options received ticks
        since the last cycle). If None, every quotable (strike, right) is
        evaluated (periodic full-refresh path).
        """
        state = self.market_data.state
        config = self.config

        # Recalibrate SABR if needed.
        # Two triggers: (a) periodic interval since last fit, (b) underlying
        # has moved more than `sabr_fast_recal_dollars` since last fit. The
        # fast trigger keeps theo fresh during sharp moves where the periodic
        # interval would lag the market by tens of seconds.
        if config.pricing.sabr_enabled and state.underlying_price > 0:
            now = datetime.now()
            elapsed = 0
            forward_move = 0.0
            if self._last_sabr_attempt is not None:
                elapsed = (now - self._last_sabr_attempt).total_seconds()
                forward_move = abs(state.underlying_price - self._last_sabr_forward)
            fast_recal_dollars = float(getattr(
                config.pricing, "sabr_fast_recal_dollars", 10.0))
            should_recal = (
                self._last_sabr_attempt is None
                or elapsed >= config.pricing.sabr_recalibrate_seconds
                or forward_move >= fast_recal_dollars
            )
            if should_recal:
                self._last_sabr_attempt = now
                self._last_sabr_forward = state.underlying_price
                self.sabr.set_expiry(state.front_month_expiry)
                self.sabr.calibrate(state.underlying_price, state.options)

        quotable = self.market_data.get_quotable_strikes()  # list of (strike, right)
        quotable_set = set(quotable)

        if dirty is not None:
            iter_pairs = [pair for pair in quotable if pair in dirty]
        else:
            iter_pairs = quotable

        # Precompute our resting prices once per cycle. Avoids walking
        # ib.openTrades() twice per (strike, right) pair inside the loop.
        our_prices_idx = self._build_our_prices_index()
        _empty_set: set = set()

        for strike, right in iter_pairs:
            option = state.get_option(strike, right=right)
            if option is None:
                continue

            self._decision_tick_ns[(strike, right)] = option.tick_received_ns

            # Theo from SABR (per right)
            theo = None
            if config.pricing.sabr_enabled and self.sabr.last_calibration is not None:
                try:
                    theo = self.sabr.get_theo(strike, right)
                except Exception:
                    theo = None

            our_prices = our_prices_idx.get((strike, right), _empty_set)
            inc_bid_info = self.market_data.find_incumbent(strike, "BUY", our_prices, right=right)
            inc_ask_info = self.market_data.find_incumbent(strike, "SELL", our_prices, right=right)

            self._process_side(portfolio, option, strike, right, "BUY",
                               inc_bid_info, theo)
            self._process_side(portfolio, option, strike, right, "SELL",
                               inc_ask_info, theo)

        # On full refresh, sweep stale orders for any (strike, right, side) that
        # is no longer quotable.
        if dirty is None:
            for (strike, right, side) in list(self.active_orders.keys()):
                if (strike, right) not in quotable_set:
                    self._cancel_quote(strike, right, side)

    def _process_side(self, portfolio, option, strike: float, right: str,
                      side: str, inc_info: dict, theo: Optional[float]) -> None:
        """Apply skip-reason gate, theo-edge gate, constraint check, and
        order placement for a single (strike, right, side). Centralizes the
        bid/ask logic that used to be duplicated in update_quotes."""
        config = self.config
        tick = config.quoting.tick_size

        # Skip-reason gate from market_data
        if inc_info["skip_reason"]:
            # self_only = our order is the only level on this side; leave it
            # in place rather than cancelling and losing queue priority.
            if inc_info["skip_reason"] != "self_only":
                self._cancel_quote(strike, right, side)
            self._log_quote_telemetry(strike, right, side, None, inc_info, theo=theo)
            return

        # Penny-jump the incumbent
        if side == "BUY":
            jumped = inc_info["price"] + (tick * config.quoting.penny_jump_ticks)
        else:
            jumped = inc_info["price"] - (tick * config.quoting.penny_jump_ticks)
        adj = round_to_tick(jumped, tick)

        # Theo edge gate: reject if our price wouldn't sit min_edge_points
        # away from theo on the favorable side. Hysteresis: require N
        # consecutive violations on the same key before cancelling so a
        # single boundary tick doesn't churn a resting order. Default N=1
        # (no hysteresis); set theo_edge_hysteresis_ticks: 2 in config to
        # smooth at the cost of slightly higher behind% on stale quotes.
        key = (strike, right, side)
        if theo is not None and config.pricing.min_edge_points > 0:
            edge = config.pricing.min_edge_points
            hyst = int(getattr(config.pricing, "theo_edge_hysteresis_ticks", 1))
            violates = (adj > theo - edge) if side == "BUY" else (adj < theo + edge)
            if violates:
                streak = self._theo_edge_streak.get(key, 0) + 1
                self._theo_edge_streak[key] = streak
                if streak >= hyst:
                    self._cancel_quote(strike, right, side)
                self._log_quote_telemetry(strike, right, side, None,
                                          {**inc_info, "skip_reason": "theo_edge"},
                                          theo=theo)
                return
            else:
                self._theo_edge_streak.pop(key, None)

        if adj <= 0:
            self._cancel_quote(strike, right, side)
            self._log_quote_telemetry(strike, right, side, None,
                                      {**inc_info, "skip_reason": "invalid_price"},
                                      theo=theo)
            return

        can_quote, reason = should_quote_side(
            portfolio, option, side, adj,
            self.constraint_checker, self.sabr, config,
        )
        if can_quote:
            self._send_or_update(strike, right, side, adj,
                                 config.product.quote_size, option)
            self._log_quote_telemetry(strike, right, side, adj, inc_info, theo=theo)
        else:
            self._cancel_quote(strike, right, side)
            self._log_rejection(strike, right, side, reason)
            self._log_quote_telemetry(strike, right, side, None,
                                      {**inc_info, "skip_reason": reason},
                                      theo=theo)

    def _is_order_live(self, trade) -> bool:
        """Check if an order is still active (not dead/cancelled/filled)."""
        if trade is None:
            return False
        status = trade.orderStatus.status
        return status in ("PendingSubmit", "PreSubmitted", "Submitted")

    def _send_or_update(self, strike: float, right: str, side: str,
                        price: float, qty: int, option):
        """Send new order or modify existing if price changed."""
        key = (strike, right, side)
        contract = option.contract

        if contract is None:
            logger.warning("No contract for %s%s, cannot send order", int(strike), right)
            return

        if key in self.active_orders:
            order_id = self.active_orders[key]
            trade = self._canonical_trade(order_id)

            if not self._is_order_live(trade):
                self.active_orders.pop(key, None)
            elif trade.order.lmtPrice != price:
                # Modify in place. We used to throttle this with a per-key
                # cooldown to dodge Error 103 races, but the real amend-ack
                # latency (~100ms p50, 460ms p90) far exceeds any client-side
                # throttle we'd want to set, so the cooldown was protecting
                # against nothing while adding stale-quote dwell time.
                now_ns = time.monotonic_ns()
                trade.order.lmtPrice = price
                trade.order.totalQuantity = qty
                trade.order.goodTillDate = _gtd_string()
                self._record_send_latency(strike, right, trade.order.orderId)
                # Stash for amend-RTT capture: record the price we just sent
                # and the timestamp. _capture_rtt_from_log will record the
                # latency once it observes the canonical Trade reflect this
                # new price. Bounded so it can't leak.
                self._pending_amend[trade.order.orderId] = (price, now_ns)
                if len(self._pending_amend) > TRACKING_DICT_MAX:
                    self._evict_oldest_half(self._pending_amend)
                self.ib.placeOrder(trade.contract, trade.order)
                return
            else:
                gtd_str = trade.order.goodTillDate or ""
                remaining = float("inf")
                try:
                    gtd_clean = gtd_str.replace(" UTC", "")
                    gtd_dt = datetime.strptime(gtd_clean, "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    remaining = (gtd_dt - datetime.now(tz=timezone.utc)).total_seconds()
                except Exception:
                    remaining = 0
                if remaining < GTD_REFRESH_THRESHOLD_SECONDS:
                    trade.order.goodTillDate = _gtd_string()
                    self.ib.placeOrder(trade.contract, trade.order)
                return

        # Place a new order
        action = "BUY" if side == "BUY" else "SELL"
        # account= is REQUIRED on multi-account logins (DFP/DUP paper sub-
        # accounts) — IBKR returns Error 436 "You must specify an allocation"
        # if it's missing. Verified 2026-04-08.
        order = LimitOrder(
            action=action,
            totalQuantity=qty,
            lmtPrice=price,
            tif="GTD",
            goodTillDate=_gtd_string(),
            account=self._account,
            orderRef=f"{ORDER_REF_PREFIX}_{int(strike)}{right}_{side}",
        )
        trade = self.ib.placeOrder(contract, order)
        self.active_orders[key] = trade.order.orderId
        self._record_send_latency(strike, right, trade.order.orderId)

    @staticmethod
    def _evict_oldest_half(d: dict) -> None:
        """Drop the oldest half of a dict in place. Cheap stand-in for an
        LRU eviction policy on the latency tracking dicts. Relies on Python
        3.7+ insertion-order semantics: keys() returns them in insert order,
        so the first half is the oldest. O(N) per eviction, called at most
        once every TRACKING_DICT_MAX inserts → amortized O(1)."""
        keys = list(d.keys())
        for k in keys[: len(keys) // 2]:
            d.pop(k, None)

    def _record_send_latency(self, strike: float, right: str, order_id: int):
        """Capture TTT (tick→placeOrder) and arm RTT/fill timers for this order.

        TTT is only sampled when the underlying tick is fresh (<50ms old).
        Older ticks come from the periodic 1s fallback refresh, where the
        recorded delta is data age rather than compute latency.

        Two timers are armed:
          - _pending_rtt: cleared on Submitted ack (used for RTT histogram)
          - _placed_at_ns: cleared on fill (used for fill latency in fills.csv)

        Both dicts are capped at TRACKING_DICT_MAX so they can't leak when
        an order goes terminal without an observation (GTD-expired before
        the next snapshot, cancelled by the broker, etc).
        """
        now_ns = time.monotonic_ns()
        tick_ns = self._decision_tick_ns.get((strike, right), 0)
        if tick_ns > 0:
            ttt_us = (now_ns - tick_ns) // 1000
            if 0 <= ttt_us < 50_000:
                self._ttt_us.append(ttt_us)
        self._pending_rtt[order_id] = now_ns
        if len(self._pending_rtt) > TRACKING_DICT_MAX:
            self._evict_oldest_half(self._pending_rtt)
        # Only record the FIRST place time per order — modifies don't reset
        # the fill clock, since the same order id can fill at any moment
        # after the original submission.
        if order_id not in self._placed_at_ns:
            self._placed_at_ns[order_id] = now_ns
            if len(self._placed_at_ns) > TRACKING_DICT_MAX:
                self._evict_oldest_half(self._placed_at_ns)

    def fill_latency_ms(self, order_id: int) -> Optional[float]:
        """Return milliseconds from first placement to now for an order, or
        None if we don't have a record. Caller is responsible for invoking
        this in the fill handler at the moment a fill arrives."""
        placed_ns = self._placed_at_ns.pop(order_id, None)
        if placed_ns is None:
            return None
        return (time.monotonic_ns() - placed_ns) / 1_000_000.0

    def _capture_rtt_from_log(self, trade):
        """Capture place-RTT and amend-RTT for an order.

        Two distinct measurements:

        place-RTT: time from placeOrder(new) to first observation of the
            canonical Trade past PendingSubmit. Captured once per orderId.

        amend-RTT: time from placeOrder(modify) to observing the canonical
            Trade's lmtPrice match the value we just sent. Captured once per
            (orderId, sent_price) pair so back-to-back amends each get
            measured independently.

        We deliberately do NOT use trade.log timestamps because the canonical
        Trade from openTrades/openOrder doesn't preserve the local
        PendingSubmit timestamp — that only exists on the orphan Trade
        returned by placeOrder.
        """
        try:
            oid = trade.order.orderId
            now_ns = time.monotonic_ns()

            # ── place-RTT ─────────────────────────────────────────
            if oid not in self._rtt_captured_oids:
                status = trade.orderStatus.status
                # PendingSubmit means we haven't been acked yet.
                if status not in ("PendingSubmit", "ApiPending", "Inactive", ""):
                    sent_ns = self._pending_rtt.pop(oid, None)
                    if sent_ns is not None:
                        rtt_us = (now_ns - sent_ns) // 1000
                        if 0 <= rtt_us < 5_000_000:
                            self._rtt_us.append(rtt_us)
                        self._rtt_captured_oids.add(oid)
                        # Same bound pattern as the latency tracking dicts.
                        if len(self._rtt_captured_oids) > TRACKING_DICT_MAX:
                            self._rtt_captured_oids = set(
                                list(self._rtt_captured_oids)[-(TRACKING_DICT_MAX // 2):]
                            )

            # ── amend-RTT ─────────────────────────────────────────
            pending = self._pending_amend.get(oid)
            if pending is not None:
                sent_price, sent_ns = pending
                # Has the canonical Trade caught up to the price we sent?
                if abs(trade.order.lmtPrice - sent_price) < 1e-9:
                    amend_us = (now_ns - sent_ns) // 1000
                    if 0 <= amend_us < 5_000_000:
                        self._amend_us.append(amend_us)
                    self._pending_amend.pop(oid, None)
        except Exception:
            pass

    def get_latency_snapshot(self) -> dict:
        """Return rolling p50/p90/p99 in microseconds for TTT, RTT, and amend."""
        def stats(buf):
            if not buf:
                return {"n": 0, "p50": None, "p90": None, "p99": None}
            s = sorted(buf)
            n = len(s)
            return {
                "n": n,
                "p50": s[min(n - 1, int(n * 0.50))],
                "p90": s[min(n - 1, int(n * 0.90))],
                "p99": s[min(n - 1, int(n * 0.99))],
            }
        return {
            "ttt_us": stats(self._ttt_us),
            "rtt_us": stats(self._rtt_us),
            "amend_us": stats(self._amend_us),
        }

    def _cancel_quote(self, strike: float, right: str, side: str):
        """Cancel a quote at a specific (strike, right, side).

        Skips cancellation when the order was placed less than
        MIN_ORDER_LIFETIME_MS ago AND is still in PendingSubmit (i.e., IBKR
        hasn't acked yet). Cancelling in that window produces a
        PendingSubmit → PendingCancel transition that never visits Submitted,
        so the order is invisible on the book and we burn a place/cancel pair
        for nothing. The next quote tick will re-evaluate and cancel then if
        the skip condition still holds.
        """
        key = (strike, right, side)
        if key not in self.active_orders:
            return
        order_id = self.active_orders[key]
        trade = self._canonical_trade(order_id)
        if trade is not None:
            placed_ns = self._placed_at_ns.get(order_id)
            status = trade.orderStatus.status
            if (placed_ns is not None
                    and status in ("PendingSubmit", "ApiPending")
                    and (time.monotonic_ns() - placed_ns) < MIN_ORDER_LIFETIME_MS * 1_000_000):
                # Too young to cancel — let IBKR ack first.
                return
            self.ib.cancelOrder(trade.order)
        del self.active_orders[key]

    def cancel_all_quotes(self):
        """Kill switch: cancel everything immediately."""
        for key, order_id in list(self.active_orders.items()):
            trade = self._canonical_trade(order_id)
            if trade is not None:
                try:
                    self.ib.cancelOrder(trade.order)
                except Exception as e:
                    logger.warning("Failed to cancel order %d: %s", order_id, e)
        self.active_orders.clear()
        logger.info("All quotes cancelled")

    def _log_quote_telemetry(self, strike: float, right: str, side: str,
                             our_price: Optional[float], info: dict,
                             theo: Optional[float] = None):
        """Emit per-quote telemetry row."""
        if self.csv_logger is None or not self.config.logging.log_quotes:
            return
        try:
            self.csv_logger.log_quote(
                strike=strike, side=side, our_price=our_price,
                incumbent_price=info.get("price"),
                incumbent_level=info.get("level"),
                incumbent_size=info.get("size"),
                incumbent_age_ms=info.get("age_ms"),
                bbo_width=info.get("bbo_width"),
                skip_reason=info.get("skip_reason", ""),
                theo=theo,
                put_call=right,
            )
        except Exception as e:
            logger.debug("quote telemetry log failed: %s", e)

    def _log_rejection(self, strike: float, right: str, side: str, reason: str):
        """Log a quote rejection."""
        if self.config.logging.log_rejections:
            logger.info("REJECT %s %d%s: %s", side, int(strike), right, reason)

    @property
    def active_quote_count(self) -> int:
        return len(self.active_orders)

    def get_active_quotes(self) -> Dict:
        """Return dict keyed by (strike, right, side) -> live quote info.

        Refreshes the canonical-trade index up front so the per-order lookups
        below are O(1) instead of O(N) per call. This is the snapshot writer's
        4Hz hot path; without the refresh we'd walk openTrades once per
        active order per snapshot (~50 × 200 = 10K iterations every 250ms).
        """
        # Side-effect: populates self._canonical_idx that _canonical_trade reads.
        self._build_our_prices_index()
        quotes = {}
        for (strike, right, side), order_id in self.active_orders.items():
            trade = self._canonical_trade(order_id)
            if trade is not None:
                # Opportunistically extract RTT (place + amend) the first time
                # we observe a post-ack state for this orderId.
                self._capture_rtt_from_log(trade)
                quotes[(strike, right, side)] = {
                    "order_id": order_id,
                    "price": trade.order.lmtPrice,
                    "qty": trade.order.totalQuantity,
                    "status": trade.orderStatus.status if hasattr(trade, 'orderStatus') else "unknown",
                }
        return quotes


