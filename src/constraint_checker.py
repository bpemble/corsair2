"""Constraint checker for Corsair v2.

Pre-trade validation ensuring every potential fill passes:
1. SPAN margin <= margin ceiling   (synthetic SPAN)
2. |Net delta| <= delta ceiling
3. Portfolio theta >= theta floor

Improving-fill exception: if a constraint is currently breached, allow
fills that move the state in the right direction.
"""

import logging
from typing import Dict, Tuple

from ib_insync import IB

from .synthetic_span import SyntheticSpan
from .utils import days_to_expiry

logger = logging.getLogger(__name__)


def _position_tuples(positions, market_data, override=None):
    """Build (K, right, T_years, iv, qty) tuples from a position list.

    `override` (optional): a dict {(strike, expiry, right): delta_qty} to
    apply on top of the position list (used for hypothetical fills).
    """
    state = market_data.state
    out = []
    seen = set()
    for p in positions:
        key = (p.strike, p.expiry, p.put_call)
        seen.add(key)
        opt = state.get_option(p.strike, p.expiry, p.put_call)
        iv = float(opt.iv) if opt and opt.iv > 0 else 0.0
        T = max(days_to_expiry(p.expiry), 0) / 365.0
        qty = p.quantity + (override.get(key, 0) if override else 0)
        if qty != 0:
            out.append((p.strike, p.put_call, T, iv, qty))
    if override:
        for key, dq in override.items():
            if key in seen or dq == 0:
                continue
            strike, expiry, right = key
            opt = state.get_option(strike, expiry, right)
            iv = float(opt.iv) if opt and opt.iv > 0 else 0.0
            T = max(days_to_expiry(expiry), 0) / 365.0
            out.append((strike, right, T, iv, dq))
    return out


class IBKRMarginChecker:
    """Synthetic SPAN margin checker.

    Computes margin via SyntheticSpan over the live position book.
    `update_cached_margin()` queries IBKR's actual MaintMarginReq for
    reconciliation logging — synthetic vs real drift is logged so you
    can recalibrate the scan percentages.
    """

    def __init__(self, ib: IB, config, market_data, portfolio):
        self.ib = ib
        self.config = config
        self.market_data = market_data
        self.portfolio = portfolio
        self.span = SyntheticSpan(config)
        self.cached_ibkr_margin: float = 0.0
        self._account_id = config.account.account_id

    def get_current_margin(self) -> float:
        """Synthetic SPAN margin over current positions."""
        F = self.market_data.state.underlying_price
        if F <= 0:
            return 0.0
        positions = _position_tuples(self.portfolio.positions, self.market_data)
        return self.span.portfolio_margin(F, positions)["total_margin"]

    def update_cached_margin(self):
        """Refresh IBKR's reported maintenance margin (for reconciliation)."""
        try:
            for item in self.ib.accountValues(self._account_id):
                if item.tag == "MaintMarginReq" and item.currency == "USD":
                    self.cached_ibkr_margin = float(item.value)
                    synth = self.get_current_margin()
                    if synth > 0 and self.cached_ibkr_margin > 0:
                        logger.info(
                            "MARGIN RECON: synthetic=$%.0f ibkr=$%.0f ratio=%.2f",
                            synth, self.cached_ibkr_margin, synth / self.cached_ibkr_margin,
                        )
                    return
        except Exception as e:
            logger.warning("Failed to refresh IBKR margin: %s", e)

    def check_fill_margin(self, option_quote, quantity: int) -> Dict:
        """Compute current and post-fill synthetic SPAN margin."""
        F = self.market_data.state.underlying_price
        if F <= 0:
            return {"current_margin": 0.0, "post_fill_margin": 0.0}

        positions = self.portfolio.positions
        cur = self.span.portfolio_margin(
            F, _position_tuples(positions, self.market_data),
        )["total_margin"]

        override = {(option_quote.strike, option_quote.expiry, option_quote.put_call): quantity}
        post = self.span.portfolio_margin(
            F, _position_tuples(positions, self.market_data, override=override),
        )["total_margin"]

        return {"current_margin": cur, "post_fill_margin": post}


class ConstraintChecker:
    """Pre-trade constraint validation: margin + delta + theta."""

    def __init__(self, margin_checker, portfolio, config):
        self.margin = margin_checker
        self.portfolio = portfolio
        self.config = config

    def check_constraints(self, option_quote, side: str,
                          quantity: int = 1) -> Tuple[bool, str]:
        """Check if a hypothetical fill passes all constraints.

        Improving-fill exception: if currently breached, allow fills that
        move state in the right direction.
        """
        fill_qty = quantity if side == "BUY" else -quantity
        multiplier = self.config.product.multiplier

        # ── Constraint 1: SPAN margin ───────────────────────────────
        margin_ceiling = (self.config.constraints.capital
                          * self.config.constraints.margin_ceiling_pct)
        margin_result = self.margin.check_fill_margin(option_quote, fill_qty)
        cur_margin = margin_result["current_margin"]
        post_margin = margin_result["post_fill_margin"]
        if post_margin > margin_ceiling:
            if not (cur_margin > margin_ceiling and post_margin <= cur_margin):
                return False, f"margin (${post_margin:,.0f} > ${margin_ceiling:,.0f})"

        # ── Constraint 2: Net delta ─────────────────────────────────
        delta_ceiling = self.config.constraints.delta_ceiling
        cur_delta = self.portfolio.net_delta
        post_delta = cur_delta + (option_quote.delta * fill_qty)
        if abs(post_delta) > delta_ceiling:
            if not (abs(cur_delta) > delta_ceiling and abs(post_delta) < abs(cur_delta)):
                return False, f"delta ({post_delta:+.2f} > ±{delta_ceiling})"

        # ── Constraint 3: Portfolio theta ───────────────────────────
        option_theta = option_quote.theta * multiplier
        cur_theta = self.portfolio.net_theta
        post_theta = cur_theta + (option_theta * fill_qty)
        theta_floor = self.config.constraints.theta_floor
        if post_theta < theta_floor:
            if not (cur_theta < theta_floor and post_theta >= cur_theta):
                return False, f"theta (${post_theta:,.0f} < ${theta_floor:,.0f})"

        return True, "ok"
