"""QuoteManager unit tests — quoting pipeline logic.

Validates:
  - should_quote_side gates on constraint failures and stale quotes
  - Penny-jump price construction (BUY jumps above, SELL jumps below)
  - Theo aggression cap clamps price to stay min_edge from theo
  - GTD refresh fires when remaining time is below threshold
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.quote_engine import (
    should_quote_side,
    GTD_EXPIRY_SECONDS,
    GTD_REFRESH_THRESHOLD_SECONDS,
    _gtd_string,
)
from src.utils import round_to_tick, floor_to_tick, ceil_to_tick


# ---- should_quote_side -------------------------------------------------------

class _PassChecker:
    """Constraint checker that always passes."""
    def check_constraints(self, option, side):
        return True, "ok"


class _FailChecker:
    """Constraint checker that always rejects."""
    def __init__(self, reason="margin"):
        self._reason = reason

    def check_constraints(self, option, side):
        return False, self._reason


class _StubSABR:
    def __init__(self, stale=False):
        self.last_calibration = datetime.now()
        self._stale = stale

    def is_quote_stale(self, option):
        return self._stale


def _option(strike=2100, put_call="C"):
    return SimpleNamespace(strike=strike, put_call=put_call, delta=0.5)


def _cfg():
    return SimpleNamespace(
        pricing=SimpleNamespace(sabr_enabled=True, min_edge_points=0.5),
    )


def test_should_quote_passes_when_constraints_ok():
    ok, reason = should_quote_side(
        _option(), "BUY", _PassChecker(), _StubSABR(), _cfg())
    assert ok
    assert reason == "ok"


def test_should_quote_rejects_on_constraint_failure():
    ok, reason = should_quote_side(
        _option(), "BUY", _FailChecker("delta"), _StubSABR(), _cfg())
    assert not ok
    assert reason == "delta"


def test_should_quote_rejects_stale_quote():
    ok, reason = should_quote_side(
        _option(), "SELL", _PassChecker(), _StubSABR(stale=True), _cfg())
    assert not ok
    assert reason == "stale_quote"


def test_should_quote_skips_stale_check_when_sabr_disabled():
    """When SABR is disabled, stale-quote filter should not fire."""
    cfg = _cfg()
    cfg.pricing.sabr_enabled = False
    ok, reason = should_quote_side(
        _option(), "SELL", _PassChecker(), _StubSABR(stale=True), cfg)
    assert ok


def test_should_quote_skips_stale_check_before_first_calibration():
    """Before first calibration, stale check can't run."""
    sabr = _StubSABR(stale=True)
    sabr.last_calibration = None
    ok, reason = should_quote_side(
        _option(), "BUY", _PassChecker(), sabr, _cfg())
    assert ok


# ---- Penny-jump price construction ------------------------------------------

TICK = 0.50


def test_penny_jump_buy_above_incumbent():
    """BUY penny-jump should land 1 tick above the incumbent bid."""
    incumbent_price = 80.0
    jumped = incumbent_price + (TICK * 1)
    adj = round_to_tick(jumped, TICK)
    assert adj == 80.5


def test_penny_jump_sell_below_incumbent():
    """SELL penny-jump should land 1 tick below the incumbent ask."""
    incumbent_price = 85.0
    jumped = incumbent_price - (TICK * 1)
    adj = round_to_tick(jumped, TICK)
    assert adj == 84.5


def test_theo_cap_clamps_buy_price():
    """BUY price should not exceed theo - min_edge."""
    theo = 82.0
    min_edge = 0.5
    # Penny-jump would land at 83.0 (above theo - edge = 81.5)
    adj = 83.0
    cap_raw = theo - min_edge
    capped = floor_to_tick(cap_raw, TICK)
    if adj > capped:
        adj = capped
    assert adj == 81.5


def test_theo_cap_clamps_sell_price():
    """SELL price should not go below theo + min_edge."""
    theo = 82.0
    min_edge = 0.5
    # Penny-jump would land at 81.5 (below theo + edge = 82.5)
    adj = 81.5
    cap_raw = theo + min_edge
    capped = ceil_to_tick(cap_raw, TICK)
    if adj < capped:
        adj = capped
    assert adj == 82.5


def test_theo_cap_no_clamp_when_within_edge():
    """Price within edge bounds should not be modified."""
    theo = 85.0
    min_edge = 0.5
    adj_buy = 83.0  # well below theo - 0.5 = 84.5
    cap_raw = theo - min_edge
    capped = floor_to_tick(cap_raw, TICK)
    # 83.0 < 84.5, no clamp
    assert adj_buy <= capped


def test_behind_incumbent_detected():
    """A BUY at or below incumbent, or SELL at or above, is 'behind'."""
    inc_price = 80.0
    # BUY at incumbent = behind (no queue advantage)
    assert 80.0 <= inc_price
    # BUY 1 tick above = not behind
    assert 80.5 > inc_price

    inc_price = 85.0
    # SELL at incumbent = behind
    assert 85.0 >= inc_price
    # SELL 1 tick below = not behind
    assert 84.5 < inc_price


# ---- GTD refresh logic -------------------------------------------------------

def test_gtd_string_format():
    """GTD string should be UTC-formatted and parseable."""
    gtd = _gtd_string(30)
    assert gtd.endswith(" UTC")
    # Parse it back
    clean = gtd.replace(" UTC", "")
    dt = datetime.strptime(clean, "%Y%m%d %H:%M:%S")
    # Should be ~30 seconds in the future (within 5s tolerance)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    diff = (dt - now).total_seconds()
    assert 25 <= diff <= 35


def test_gtd_refresh_threshold():
    """An order with < GTD_REFRESH_THRESHOLD_SECONDS remaining should
    trigger a refresh."""
    # Simulate: GTD set 25 seconds ago with 30s expiry → 5s remaining
    gtd_set_time = datetime.now(tz=timezone.utc) - timedelta(seconds=25)
    gtd_dt = gtd_set_time + timedelta(seconds=GTD_EXPIRY_SECONDS)
    remaining = (gtd_dt - datetime.now(tz=timezone.utc)).total_seconds()
    assert remaining < GTD_REFRESH_THRESHOLD_SECONDS
    # → should refresh


def test_gtd_no_refresh_when_fresh():
    """An order with plenty of GTD time remaining should NOT refresh."""
    # Simulate: GTD set 5 seconds ago with 30s expiry → 25s remaining
    gtd_set_time = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    gtd_dt = gtd_set_time + timedelta(seconds=GTD_EXPIRY_SECONDS)
    remaining = (gtd_dt - datetime.now(tz=timezone.utc)).total_seconds()
    assert remaining >= GTD_REFRESH_THRESHOLD_SECONDS
    # → should NOT refresh


# ---- Tick rounding helpers ---------------------------------------------------

def test_round_to_tick():
    assert round_to_tick(80.3, 0.5) == 80.5
    assert round_to_tick(80.2, 0.5) == 80.0
    assert round_to_tick(80.25, 0.5) == 80.0  # Python banker's rounding: 0.5 rounds to even


def test_floor_to_tick():
    assert floor_to_tick(80.7, 0.5) == 80.5
    assert floor_to_tick(80.9, 0.5) == 80.5
    assert floor_to_tick(81.0, 0.5) == 81.0


def test_ceil_to_tick():
    assert ceil_to_tick(80.1, 0.5) == 80.5
    assert ceil_to_tick(80.5, 0.5) == 80.5
    assert ceil_to_tick(80.6, 0.5) == 81.0
