"""OperationalKillMonitor tests.

Covers the sustained-breach pattern (``first_breach_ts``) which replaced
an earlier sliding-window check whose "oldest sample within 1s of
cutoff" gate silently failed at moderately-slow check cadences.
"""

from types import SimpleNamespace

import pytest

from src.operational_kills import OperationalKillMonitor


class _StubRisk:
    def __init__(self):
        self.killed = False
        self.kill_calls: list = []

    def kill(self, reason, source="risk", kill_type="halt"):
        self.killed = True
        self.kill_calls.append((reason, source, kill_type))


class _StubSabr:
    def __init__(self, rmse: float = None):
        self._rmse = rmse

    def latest_rmse(self, expiry: str):
        return self._rmse


class _StubQuotes:
    def __init__(self, p50_us: float = None):
        self._p50_us = p50_us

    def get_latency_snapshot(self):
        return {"place_rtt_us": {"p50": self._p50_us}}


def _make_monitor(rmse=None, p50_us=None, enabled=True,
                  rmse_threshold=0.05, rmse_window_sec=300.0,
                  latency_ms_max=2000.0, latency_window_sec=60.0):
    """Build an OperationalKillMonitor with a single synthetic engine."""
    config = SimpleNamespace(
        operational_kills=SimpleNamespace(
            enabled=enabled,
            sabr_rmse_threshold=rmse_threshold,
            sabr_rmse_window_sec=rmse_window_sec,
            quote_latency_max_ms=latency_ms_max,
            quote_latency_window_sec=latency_window_sec,
        )
    )
    engines = [{
        "name": "HG",
        "sabr": _StubSabr(rmse=rmse),
        "md": SimpleNamespace(state=SimpleNamespace(expiries=["20260427"])),
        "quotes": _StubQuotes(p50_us=p50_us),
    }]
    portfolio = SimpleNamespace(fills_today=0)
    risk = _StubRisk()
    return OperationalKillMonitor(engines, portfolio, risk, config), risk


# ── RMSE sustained-breach pattern ─────────────────────────────────────

def test_rmse_single_sample_does_not_fire():
    """One breach sample starts a streak but doesn't fire — window_sec
    hasn't elapsed yet."""
    mon, risk = _make_monitor(rmse=0.1, rmse_window_sec=10.0)
    mon._check_sabr_rmse(now=1000.0)
    assert not risk.killed
    # Streak started
    assert mon._rmse_first_breach_ts[("HG", "20260427")] == 1000.0


def test_rmse_sustained_breach_fires_after_window():
    """Consecutive breaches spanning more than window_sec fire the kill."""
    mon, risk = _make_monitor(rmse=0.1, rmse_window_sec=10.0)
    mon._check_sabr_rmse(now=1000.0)
    assert not risk.killed
    mon._check_sabr_rmse(now=1015.0)  # 15s later, still breaching
    assert risk.killed
    assert risk.kill_calls[0][1] == "operational"


def test_rmse_good_sample_resets_streak():
    """A good sample (rmse ≤ threshold) resets the streak — next breach
    must re-accumulate from scratch before firing."""
    mon, _ = _make_monitor(rmse=0.1, rmse_window_sec=10.0)
    mon._check_sabr_rmse(now=1000.0)
    # Flip to a good sample
    mon.engines[0]["sabr"]._rmse = 0.01
    mon._check_sabr_rmse(now=1005.0)
    assert mon._rmse_first_breach_ts[("HG", "20260427")] is None

    # Breach resumes — fresh streak
    mon.engines[0]["sabr"]._rmse = 0.1
    mon._check_sabr_rmse(now=1010.0)
    assert mon._rmse_first_breach_ts[("HG", "20260427")] == 1010.0


def test_rmse_none_does_not_start_streak():
    """Pre-fit state (rmse is None) is NOT a breach — streak stays
    unarmed. A fresh SABR surface must not tag the kill."""
    mon, risk = _make_monitor(rmse=None, rmse_window_sec=10.0)
    mon._check_sabr_rmse(now=1000.0)
    mon._check_sabr_rmse(now=1015.0)
    assert not risk.killed
    assert ("HG", "20260427") not in mon._rmse_first_breach_ts


# ── Latency sustained-breach pattern ──────────────────────────────────

def test_latency_sustained_breach_fires_after_window():
    """Median place-RTT above threshold for window_sec triggers kill."""
    mon, risk = _make_monitor(p50_us=3_000_000,  # 3000ms
                              latency_ms_max=2000.0, latency_window_sec=30.0)
    mon._check_quote_latency(now=500.0)
    assert not risk.killed
    mon._check_quote_latency(now=535.0)
    assert risk.killed


def test_latency_good_sample_resets_streak():
    """p50 drops below threshold → streak resets → next breach
    starts fresh."""
    mon, risk = _make_monitor(p50_us=3_000_000,
                              latency_ms_max=2000.0, latency_window_sec=30.0)
    mon._check_quote_latency(now=500.0)
    assert mon._latency_first_breach_ts == 500.0
    mon.engines[0]["quotes"]._p50_us = 800_000  # 800ms
    mon._check_quote_latency(now=510.0)
    assert mon._latency_first_breach_ts is None
    assert not risk.killed


def test_latency_skips_when_no_samples_yet():
    """get_latency_snapshot returns p50=None pre-samples; the check
    must skip without starting a streak."""
    mon, _ = _make_monitor(p50_us=None)
    mon._check_quote_latency(now=500.0)
    assert mon._latency_first_breach_ts is None


# ── Disabled / safety ─────────────────────────────────────────────────

def test_disabled_monitor_is_no_op():
    """config.operational_kills.enabled=False → check() returns without
    touching streak state."""
    mon, risk = _make_monitor(rmse=0.1, enabled=False)
    mon.check()
    assert not risk.killed
    assert mon._rmse_first_breach_ts == {}


def test_already_killed_monitor_is_no_op():
    """If risk is already killed, check() bails before any detection —
    operational kill layers on top, doesn't re-fire."""
    mon, risk = _make_monitor(rmse=0.1)
    risk.killed = True
    mon.check()
    # No additional kill call — it was already killed before entry.
    assert len(risk.kill_calls) == 0
