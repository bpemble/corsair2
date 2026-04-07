"""Quote engine for Corsair v2.

For each quotable strike:
  - Compute bid/ask: penny-jump incumbent, bounded by theo ± buffer
  - Check that a hypothetical fill passes constraints (margin/delta/theta)
  - Send/update or cancel
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from ib_insync import IB, LimitOrder

from .utils import round_to_tick

logger = logging.getLogger(__name__)

OrderKey = Tuple[float, str]  # (strike, side)
ORDER_REF_PREFIX = "corsair2"

# Orders auto-cancel after this many seconds if not refreshed.
# Protects against bot crashes, container kills, network outages.
GTD_EXPIRY_SECONDS = 60
# Only re-send the order to refresh GTD when less than this many seconds
# remain. With a 60s expiry and 15s threshold, we re-send at most every
# ~45s for any single resting order whose price hasn't changed.
GTD_REFRESH_THRESHOLD_SECONDS = 15


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
        self.active_orders: Dict[OrderKey, int] = {}  # {(strike, side): order_id}
        self._order_id_to_trade: Dict[int, object] = {}  # order_id -> Trade object
        self._account = config.account.account_id
        self._last_sabr_attempt: Optional[datetime] = None
        # Last-known meaningful incumbent (for snapshot/dashboard)
        self._incumbent_bid: Dict[float, float] = {}  # strike -> incumbent bid
        self._incumbent_ask: Dict[float, float] = {}  # strike -> incumbent ask

    def _our_prices_at_strike(self, strike: float) -> set:
        """Return set of our resting limit prices at this strike for self-filter."""
        out = set()
        for side in ("BUY", "SELL"):
            oid = self.active_orders.get((strike, side))
            if oid:
                t = self._order_id_to_trade.get(oid)
                if t and self._is_order_live(t):
                    out.add(float(t.order.lmtPrice))
        return out

    def update_quotes(self, portfolio, dirty_strikes: Optional[set] = None):
        """Reprice and re-issue quotes.

        If `dirty_strikes` is provided, only those strikes are evaluated
        (event-driven path: caller has identified which options received
        ticks since the last cycle). If None, all quotable strikes are
        evaluated (periodic full-refresh path).
        """
        state = self.market_data.state
        config = self.config

        # Recalibrate SABR if needed
        if config.pricing.sabr_enabled and state.underlying_price > 0:
            now = datetime.now()
            elapsed = 0
            if self._last_sabr_attempt is not None:
                elapsed = (now - self._last_sabr_attempt).total_seconds()
            if self._last_sabr_attempt is None or elapsed >= config.pricing.sabr_recalibrate_seconds:
                self._last_sabr_attempt = now
                self.sabr.set_expiry(state.front_month_expiry)
                self.sabr.calibrate(state.underlying_price, state.options)

        quotable = self.market_data.get_quotable_strikes()

        if dirty_strikes is not None:
            iter_strikes = [s for s in quotable if s in dirty_strikes]
        else:
            iter_strikes = quotable

        for strike in iter_strikes:
            option = state.get_option(strike)
            if option is None:
                continue

            tick = config.quoting.tick_size

            # Depth-aware incumbent selection (per side)
            our_prices = self._our_prices_at_strike(strike)
            inc_bid_info = self.market_data.find_incumbent(strike, "BUY", our_prices)
            inc_ask_info = self.market_data.find_incumbent(strike, "SELL", our_prices)

            # Cache last-known meaningful incumbent for snapshot
            if inc_bid_info["price"] is not None:
                self._incumbent_bid[strike] = inc_bid_info["price"]
            if inc_ask_info["price"] is not None:
                self._incumbent_ask[strike] = inc_ask_info["price"]

            # Get theo from SABR if calibrated
            theo = None
            if config.pricing.sabr_enabled and self.sabr.last_calibration is not None:
                try:
                    theo = self.sabr.get_theo(strike, option.put_call)
                except Exception:
                    theo = None

            # ── Bid side ────────────────────────────────────────────
            if inc_bid_info["skip_reason"]:
                self._cancel_quote(strike, "BUY")
                self._log_quote_telemetry(strike, "BUY", None, inc_bid_info)
            else:
                jumped_bid = inc_bid_info["price"] + (tick * config.quoting.penny_jump_ticks)
                adj_bid = round_to_tick(jumped_bid, tick)
                # Theo edge gate: reject (don't quote) if a fill at adj_bid
                # would not be at least min_edge_points below SABR theo.
                if theo is not None and config.pricing.min_edge_points > 0:
                    if adj_bid > theo - config.pricing.min_edge_points:
                        self._cancel_quote(strike, "BUY")
                        self._log_quote_telemetry(strike, "BUY", None,
                                                  {**inc_bid_info, "skip_reason": "theo_edge"})
                        adj_bid = None
                if adj_bid is None:
                    pass  # already cancelled by theo-edge gate above
                elif adj_bid <= 0:
                    self._cancel_quote(strike, "BUY")
                    self._log_quote_telemetry(strike, "BUY", None,
                                              {**inc_bid_info, "skip_reason": "invalid_price"})
                else:
                    can_bid, reason = should_quote_side(
                        portfolio, option, "BUY", adj_bid,
                        self.constraint_checker, self.sabr, config,
                    )
                    if can_bid:
                        self._send_or_update(strike, "BUY", adj_bid,
                                             config.product.quote_size, option)
                        self._log_quote_telemetry(strike, "BUY", adj_bid, inc_bid_info)
                    else:
                        self._cancel_quote(strike, "BUY")
                        self._log_rejection(strike, "BUY", reason)
                        self._log_quote_telemetry(strike, "BUY", None,
                                                  {**inc_bid_info, "skip_reason": reason})

            # ── Ask side ────────────────────────────────────────────
            if inc_ask_info["skip_reason"]:
                self._cancel_quote(strike, "SELL")
                self._log_quote_telemetry(strike, "SELL", None, inc_ask_info)
            else:
                jumped_ask = inc_ask_info["price"] - (tick * config.quoting.penny_jump_ticks)
                adj_ask = round_to_tick(jumped_ask, tick)
                # Theo edge gate: reject if our ask is not at least
                # min_edge_points above SABR theo.
                if theo is not None and config.pricing.min_edge_points > 0:
                    if adj_ask < theo + config.pricing.min_edge_points:
                        self._cancel_quote(strike, "SELL")
                        self._log_quote_telemetry(strike, "SELL", None,
                                                  {**inc_ask_info, "skip_reason": "theo_edge"})
                        adj_ask = None
                if adj_ask is None:
                    pass  # already cancelled by theo-edge gate above
                elif adj_ask <= 0:
                    self._cancel_quote(strike, "SELL")
                    self._log_quote_telemetry(strike, "SELL", None,
                                              {**inc_ask_info, "skip_reason": "invalid_price"})
                else:
                    can_ask, reason = should_quote_side(
                        portfolio, option, "SELL", adj_ask,
                        self.constraint_checker, self.sabr, config,
                    )
                    if can_ask:
                        self._send_or_update(strike, "SELL", adj_ask,
                                             config.product.quote_size, option)
                        self._log_quote_telemetry(strike, "SELL", adj_ask, inc_ask_info)
                    else:
                        self._cancel_quote(strike, "SELL")
                        self._log_rejection(strike, "SELL", reason)
                        self._log_quote_telemetry(strike, "SELL", None,
                                                  {**inc_ask_info, "skip_reason": reason})

        # Cancel quotes on strikes no longer quotable (only on full refresh —
        # on dirty-only updates we don't have full visibility)
        if dirty_strikes is None:
            for (strike, side) in list(self.active_orders.keys()):
                if strike not in quotable:
                    self._cancel_quote(strike, side)

    def _is_order_live(self, trade) -> bool:
        """Check if an order is still active (not dead/cancelled/filled)."""
        if trade is None:
            return False
        status = trade.orderStatus.status
        return status in ("PendingSubmit", "PreSubmitted", "Submitted")

    def _send_or_update(self, strike: float, side: str, price: float,
                        qty: int, option):
        """Send new order or modify existing if price changed."""
        key = (strike, side)
        contract = option.contract

        if contract is None:
            logger.warning("No contract for strike %.0f, cannot send order", strike)
            return

        if key in self.active_orders:
            order_id = self.active_orders[key]
            trade = self._order_id_to_trade.get(order_id)

            # If the existing order is dead, clean it up and place fresh
            if not self._is_order_live(trade):
                self.active_orders.pop(key, None)
                self._order_id_to_trade.pop(order_id, None)
            elif trade.order.lmtPrice != price:
                # Modify the live order; refresh GTD on every modify
                trade.order.lmtPrice = price
                trade.order.totalQuantity = qty
                trade.order.goodTillDate = _gtd_string()
                self.ib.placeOrder(trade.contract, trade.order)
                return
            else:
                # Price unchanged. Only re-send to refresh GTD when it's
                # close to expiring — keeps API call volume low.
                gtd_str = trade.order.goodTillDate or ""
                remaining = float("inf")
                try:
                    gtd_clean = gtd_str.replace(" UTC", "")
                    gtd_dt = datetime.strptime(gtd_clean, "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    remaining = (gtd_dt - datetime.now(tz=timezone.utc)).total_seconds()
                except Exception:
                    remaining = 0  # unparseable → refresh
                if remaining < GTD_REFRESH_THRESHOLD_SECONDS:
                    trade.order.goodTillDate = _gtd_string()
                    self.ib.placeOrder(trade.contract, trade.order)
                return

        # Place a new order
        action = "BUY" if side == "BUY" else "SELL"
        order = LimitOrder(
            action=action,
            totalQuantity=qty,
            lmtPrice=price,
            tif="GTD",
            goodTillDate=_gtd_string(),
            account=self._account,
            orderRef=f"{ORDER_REF_PREFIX}_{strike}_{side}",
        )
        trade = self.ib.placeOrder(contract, order)
        self.active_orders[key] = trade.order.orderId
        self._order_id_to_trade[trade.order.orderId] = trade

    def _cancel_quote(self, strike: float, side: str):
        """Cancel a quote at a specific strike/side."""
        key = (strike, side)
        if key in self.active_orders:
            order_id = self.active_orders[key]
            trade = self._order_id_to_trade.get(order_id)
            if trade is not None:
                self.ib.cancelOrder(trade.order)
            del self.active_orders[key]
            self._order_id_to_trade.pop(order_id, None)

    def cancel_all_quotes(self):
        """Kill switch: cancel everything immediately."""
        for key, order_id in list(self.active_orders.items()):
            trade = self._order_id_to_trade.get(order_id)
            if trade is not None:
                try:
                    self.ib.cancelOrder(trade.order)
                except Exception as e:
                    logger.warning("Failed to cancel order %d: %s", order_id, e)
        self.active_orders.clear()
        self._order_id_to_trade.clear()
        logger.info("All quotes cancelled")

    def _log_quote_telemetry(self, strike: float, side: str,
                             our_price: Optional[float], info: dict):
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
            )
        except Exception as e:
            logger.debug("quote telemetry log failed: %s", e)

    def _log_rejection(self, strike: float, side: str, reason: str):
        """Log a quote rejection."""
        if self.config.logging.log_rejections:
            logger.info("REJECT %s %.0fC: %s", side, strike, reason)

    @property
    def active_quote_count(self) -> int:
        return len(self.active_orders)

    def get_active_quotes(self) -> Dict:
        """Return dict of active quotes for display."""
        quotes = {}
        for (strike, side), order_id in self.active_orders.items():
            trade = self._order_id_to_trade.get(order_id)
            if trade is not None:
                quotes[(strike, side)] = {
                    "order_id": order_id,
                    "price": trade.order.lmtPrice,
                    "qty": trade.order.totalQuantity,
                    "status": trade.orderStatus.status if hasattr(trade, 'orderStatus') else "unknown",
                }
        return quotes

    def get_incumbent(self, strike: float) -> Tuple[float, float]:
        """Return tracked incumbent (bid, ask) for a strike."""
        return (
            self._incumbent_bid.get(strike, 0.0),
            self._incumbent_ask.get(strike, 0.0),
        )

