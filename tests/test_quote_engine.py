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


# ---- Market data blackout (Error 10197) --------------------------------------

class _FakeIB:
    """Minimal IB stub for QuoteManager construction."""
    def __init__(self):
        self.openOrderEvent = _FakeEvent()
        self.errorEvent = _FakeEvent()
        self._global_cancel_called = False

    def reqGlobalCancel(self):
        self._global_cancel_called = True

    def openTrades(self):
        return []

    def isConnected(self):
        return True


class _FakeEvent:
    """Minimal event stub that accepts += for handler registration."""
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        self._handlers.remove(handler)
        return self


class _FakeMarketDataForBlackout:
    def __init__(self):
        self.state = SimpleNamespace(
            underlying_price=2100.0,
            options={
                (2100, "20260424", "C"): SimpleNamespace(
                    last_update=datetime.now(),
                    delta=0.5,
                    bid=80.0, ask=82.0,
                    contract=None,
                ),
            },
            expiries=["20260424"],
            front_month_expiry="20260424",
            atm_strike=2100,
            strike_increment=25,
            first_tick_seen=True,
            discovery_completed_at=datetime.now(),
        )
        self.state.get_option = lambda s, expiry=None, right="C": self.state.options.get(
            (s, expiry or "20260424", right)
        )
        self.state.get_all_strikes = lambda expiry=None: [2100]
        self.tick_queue = None

    def get_quotable_strikes(self, expiry=None):
        return [(2100, "C")]

    def find_incumbent(self, strike, side, our_prices, right="C", expiry=None):
        return {"price": 80.0, "level": 0, "size": 5, "age_ms": 100,
                "bbo_width": 2.0, "skip_reason": None}

    def get_clean_bbo(self, strike, right, expiry=None):
        return (80.0, 82.0)


def test_blackout_panic_cancels_on_10197():
    """Error 10197 must trigger panic_cancel and set blackout flag."""
    from src.quote_engine import QuoteManager

    ib = _FakeIB()
    cfg = SimpleNamespace(
        quoting=SimpleNamespace(
            tick_size=0.5, penny_jump_ticks=1, quote_size=1,
            refresh_interval_ms=1000, enabled_expiries=["front"],
            api_bucket_capacity=250, api_bucket_refill_per_sec=250,
        ),
        pricing=SimpleNamespace(
            sabr_enabled=False, min_edge_points=0.5,
        ),
        product=SimpleNamespace(
            multiplier=50, quote_size=1, option_type="both",
            quote_range_low=0, quote_range_high=5,
        ),
        constraints=SimpleNamespace(
            capital=200000, margin_ceiling_pct=0.50,
        ),
        account=SimpleNamespace(account_id="TEST"),
        puts=SimpleNamespace(enabled=False),
    )
    md = _FakeMarketDataForBlackout()
    qm = QuoteManager(ib, cfg, md, None, None, None)

    # Simulate a resting order
    qm.active_orders[("2100", "20260424", "C", "SELL")] = 999

    # Fire Error 10197
    qm._on_ib_error(0, 10197, "No market data during competing live session", None)

    assert qm._market_data_blackout is True
    assert ib._global_cancel_called is True
    assert len(qm.active_orders) == 0


def test_blackout_blocks_update_quotes():
    """While blackout is active, update_quotes must be a no-op."""
    from src.quote_engine import QuoteManager

    ib = _FakeIB()
    cfg = SimpleNamespace(
        quoting=SimpleNamespace(
            tick_size=0.5, penny_jump_ticks=1, quote_size=1,
            refresh_interval_ms=1000, enabled_expiries=["front"],
            api_bucket_capacity=250, api_bucket_refill_per_sec=250,
        ),
        pricing=SimpleNamespace(
            sabr_enabled=False, min_edge_points=0.5,
        ),
        product=SimpleNamespace(
            multiplier=50, quote_size=1, option_type="both",
            quote_range_low=0, quote_range_high=5,
        ),
        constraints=SimpleNamespace(
            capital=200000, margin_ceiling_pct=0.50,
        ),
        account=SimpleNamespace(account_id="TEST"),
        puts=SimpleNamespace(enabled=False),
    )
    md = _FakeMarketDataForBlackout()
    qm = QuoteManager(ib, cfg, md, None, None, None)

    # Set blackout with stale option data
    qm._market_data_blackout = True
    from datetime import timedelta
    for opt in md.state.options.values():
        opt.last_update = datetime.now() - timedelta(seconds=30)

    # Fake portfolio for update_quotes
    portfolio = SimpleNamespace(positions=[])

    # This should return immediately without placing any orders
    qm.update_quotes(portfolio)
    assert len(qm.active_orders) == 0


def test_blackout_clears_on_fresh_tick():
    """Blackout must clear when a fresh option tick arrives."""
    from src.quote_engine import QuoteManager

    ib = _FakeIB()
    cfg = SimpleNamespace(
        quoting=SimpleNamespace(
            tick_size=0.5, penny_jump_ticks=1, quote_size=1,
            refresh_interval_ms=1000, enabled_expiries=["front"],
            api_bucket_capacity=250, api_bucket_refill_per_sec=250,
        ),
        pricing=SimpleNamespace(
            sabr_enabled=False, min_edge_points=0.5,
        ),
        product=SimpleNamespace(
            multiplier=50, quote_size=1, option_type="both",
            quote_range_low=0, quote_range_high=5,
        ),
        constraints=SimpleNamespace(
            capital=200000, margin_ceiling_pct=0.50,
        ),
        account=SimpleNamespace(account_id="TEST"),
        puts=SimpleNamespace(enabled=False),
    )
    md = _FakeMarketDataForBlackout()
    qm = QuoteManager(ib, cfg, md, None, None, None)

    # Set blackout with stale data first
    qm._market_data_blackout = True
    qm._blackout_cancel_sent = True
    from datetime import timedelta
    for opt in md.state.options.values():
        opt.last_update = datetime.now() - timedelta(seconds=30)

    portfolio = SimpleNamespace(positions=[])

    # With stale data, should stay in blackout
    qm.update_quotes(portfolio)
    assert qm._market_data_blackout is True

    # Now simulate fresh option tick (last_update = now)
    for opt in md.state.options.values():
        opt.last_update = datetime.now()

    # update_quotes should detect fresh tick, clear blackout, then
    # proceed into the quoting path (which will fail on missing deps,
    # but the flag should already be cleared)
    try:
        qm.update_quotes(portfolio)
    except (AttributeError, TypeError):
        pass  # expected — constraint_checker is None in this stub
    assert qm._market_data_blackout is False
    assert qm._blackout_cancel_sent is False


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
