"""Operational kill-switch monitor for Corsair v2 (v1.4 §7).

Infrastructure-level protections that fire independently of the
strategy-level kills in RiskMonitor. These watch for failure modes that
the trading loop's health checks don't catch:

  - SABR calibration failure: RMSE > threshold sustained for N seconds
  - Quote latency breach: median place-RTT > threshold sustained for N seconds
  - Abnormal trade rate: fills/minute > multiplier × rolling-hour baseline
  - (Realized vol spike: logged but page-only per v1.4 §7; requires
    5-day historical rvol pipeline not yet wired)

IBKR disconnect and position-reconciliation failure are already wired in
main.py (on_disconnect callback) and watchdog/reconciliation code; this
module covers the remaining three.

Fires via risk.kill(reason, source="operational", kill_type="halt").
All operational kills are sticky (source="operational" is not cleared
by clear_disconnect_kill or clear_daily_halt) — they indicate a genuine
infrastructure problem that needs human review before re-quoting.
"""

import logging
import statistics
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OperationalKillMonitor:
    """Watches infrastructure health signals and trips RiskMonitor on
    sustained breaches.

    Construct ONE instance per corsair process (not per-product) because
    the kill signal is global — if SABR degrades on the primary, we
    halt everything.

    Inputs it pulls each cycle:
      - engines[*]["sabr"].last_rmse or surface fit history
      - engines[*]["quotes"]._place_rtt_us (rolling ring, microseconds)
      - portfolio.fills_today snapshots over time (rolling fill-rate)

    Public API:
      - check(): call from the main loop at 5-10s cadence
    """

    def __init__(self, engines: list, portfolio, risk_monitor, config):
        self.engines = engines
        self.portfolio = portfolio
        self.risk = risk_monitor
        self.config = config

        op = getattr(config, "operational_kills", None)
        self.enabled: bool = bool(getattr(op, "enabled", True))
        self.rmse_threshold: float = float(
            getattr(op, "sabr_rmse_threshold", 0.05))
        self.rmse_window_sec: float = float(
            getattr(op, "sabr_rmse_window_sec", 300.0))
        self.latency_ms_max: float = float(
            getattr(op, "quote_latency_max_ms", 2000.0))
        self.latency_window_sec: float = float(
            getattr(op, "quote_latency_window_sec", 60.0))
        self.fill_rate_mul: float = float(
            getattr(op, "abnormal_fill_rate_mul", 10.0))
        self.fill_rate_baseline_window_sec: float = float(
            getattr(op, "abnormal_fill_baseline_window_sec", 3600.0))
        self.rvol_alert_threshold: float = float(
            getattr(op, "rvol_alert_5d_threshold", 0.50))

        # Sliding history per signal. Each entry is (monotonic_ts, value).
        # RMSE history is keyed by (engine_name, expiry) — different
        # expiries calibrate independently so we track them separately
        # and only fire the kill when the front-month is sustained-bad.
        self._rmse_hist: Dict[Tuple[str, str], Deque[Tuple[float, float]]] = {}
        self._latency_hist: Deque[Tuple[float, float]] = deque(maxlen=2000)
        # Fill-count snapshots at each check tick: (ts, fills_today). The
        # rolling rate comes from the slope across two snapshots.
        self._fill_hist: Deque[Tuple[float, int]] = deque(maxlen=2000)

    def check(self) -> None:
        """Evaluate all operational kill switches. No-op if already
        killed. Fires risk.kill() on sustained breach."""
        if not self.enabled or self.risk.killed:
            return

        now = time.monotonic()

        # 1) SABR RMSE sustained > threshold on the front-month.
        self._check_sabr_rmse(now)
        if self.risk.killed:
            return

        # 2) Quote latency median > threshold sustained.
        self._check_quote_latency(now)
        if self.risk.killed:
            return

        # 3) Abnormal fill rate vs rolling baseline.
        self._check_fill_rate(now)

    # ── Per-switch checks ─────────────────────────────────────────────
    def _check_sabr_rmse(self, now: float) -> None:
        """Front-month RMSE > threshold continuously for window_sec ⇒ kill.

        Sampling: every check call we record one (ts, rmse) per engine's
        front expiry. If every sample inside the window exceeds the
        threshold, we fire. Single dips below the threshold reset the
        streak (implicit via the window — older good samples age out as
        the window slides forward).
        """
        cutoff = now - self.rmse_window_sec

        for eng in self.engines:
            sabr = eng.get("sabr")
            if sabr is None:
                continue
            md = eng.get("md")
            if md is None:
                continue
            # Front-month expiry only — that's what v1.4 §7 specifies.
            expiries = getattr(md.state, "expiries", []) or []
            if not expiries:
                continue
            front = expiries[0]

            # Pull the latest RMSE for this expiry. SABR stores it in the
            # per-side surface params; take the max across sides so a
            # busted call fit trips the switch even if puts are clean.
            rmse = self._latest_rmse(sabr, front)
            if rmse is None:
                continue

            key = (eng["name"], front)
            hist = self._rmse_hist.setdefault(key, deque(maxlen=2000))
            hist.append((now, rmse))
            while hist and hist[0][0] < cutoff:
                hist.popleft()

            # Fire only if the window is fully populated AND every sample
            # breaches. Window-length gate prevents a cold-start false
            # positive where two bad samples span "the whole window"
            # because the history is empty.
            if hist and hist[0][0] <= cutoff + 1.0:
                # Window is at least rmse_window_sec long AND
                # every sample breaches threshold.
                all_breached = all(r > self.rmse_threshold for _, r in hist)
                if all_breached:
                    self.risk.kill(
                        f"SABR RMSE SUSTAINED BREACH [{eng['name']}/{front}]: "
                        f"all {len(hist)} samples in {self.rmse_window_sec:.0f}s "
                        f"window > {self.rmse_threshold:.3f}",
                        source="operational", kill_type="halt",
                    )
                    return

    def _check_quote_latency(self, now: float) -> None:
        """Median place-RTT > threshold sustained ⇒ kill.

        Samples the primary engine's quote manager's _place_rtt_us ring.
        Latency above 2s indicates a wire-protocol or gateway problem
        (not a strategy issue), so we halt quoting and page operator.
        """
        cutoff = now - self.latency_window_sec
        # Primary engine only — secondaries' latency is usually correlated.
        if not self.engines:
            return
        primary = self.engines[0]
        quotes = primary.get("quotes")
        if quotes is None:
            return
        ring = getattr(quotes, "_place_rtt_us", None)
        if ring is None or len(ring) == 0:
            return

        # Convert the latest ring value to ms and stash the sample.
        # The ring holds microseconds; we don't have per-sample
        # timestamps so we just snapshot its median now.
        ring_ms = [v / 1000.0 for v in ring]
        median_ms = statistics.median(ring_ms) if ring_ms else 0.0
        self._latency_hist.append((now, median_ms))
        while self._latency_hist and self._latency_hist[0][0] < cutoff:
            self._latency_hist.popleft()

        if (self._latency_hist
                and self._latency_hist[0][0] <= cutoff + 1.0
                and all(v > self.latency_ms_max
                        for _, v in self._latency_hist)):
            self.risk.kill(
                f"QUOTE LATENCY BREACH: median place-RTT > "
                f"{self.latency_ms_max:.0f}ms sustained "
                f"{self.latency_window_sec:.0f}s (latest={median_ms:.0f}ms)",
                source="operational", kill_type="halt",
            )

    def _check_fill_rate(self, now: float) -> None:
        """Fills/minute > multiplier × baseline ⇒ kill.

        Baseline = fill-rate across the past ``baseline_window_sec``
        (default 1h). Short-term rate (last ~60s) must exceed the
        baseline by ``rate_mul`` (default 10×).

        Early-session handling: if baseline window has <2 snapshots or
        <5 total fills, skip — the ratio is meaningless when numbers
        are small.
        """
        fills_today = getattr(self.portfolio, "fills_today", 0)
        self._fill_hist.append((now, int(fills_today)))
        # Evict samples older than the baseline window + buffer.
        cutoff = now - (self.fill_rate_baseline_window_sec + 60)
        while self._fill_hist and self._fill_hist[0][0] < cutoff:
            self._fill_hist.popleft()

        if len(self._fill_hist) < 3:
            return

        # Short window: last 60s.
        short_cutoff = now - 60.0
        short_samples = [s for s in self._fill_hist if s[0] >= short_cutoff]
        if len(short_samples) < 2:
            return
        short_fills = max(0, short_samples[-1][1] - short_samples[0][1])
        short_span = max(1.0, short_samples[-1][0] - short_samples[0][0])
        short_rate_per_min = (short_fills / short_span) * 60.0

        # Baseline: past `baseline_window_sec`.
        long_cutoff = now - self.fill_rate_baseline_window_sec
        long_samples = [s for s in self._fill_hist if s[0] >= long_cutoff]
        if len(long_samples) < 2:
            return
        long_fills = max(0, long_samples[-1][1] - long_samples[0][1])
        if long_fills < 5:
            return  # not enough history to trust the ratio
        long_span = max(1.0, long_samples[-1][0] - long_samples[0][0])
        long_rate_per_min = (long_fills / long_span) * 60.0
        if long_rate_per_min <= 0:
            return

        ratio = short_rate_per_min / long_rate_per_min
        if ratio > self.fill_rate_mul:
            self.risk.kill(
                f"ABNORMAL FILL RATE: {short_rate_per_min:.1f}/min (short) "
                f"vs {long_rate_per_min:.2f}/min (hour baseline) — "
                f"ratio {ratio:.1f}× > {self.fill_rate_mul:.0f}×",
                source="operational", kill_type="halt",
            )

    # ── Helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _latest_rmse(sabr, expiry: str) -> Optional[float]:
        """Pull the latest accepted RMSE for an expiry from the SABR
        surface. Returns the max across C/P sides (bad fit on either
        side trips the switch).

        Returns None if no fit has landed yet.
        """
        # MultiExpirySABR stores per-expiry SABRSurface instances. Each
        # side params dict carries the fit result object with .rmse.
        surfaces = getattr(sabr, "surfaces", None) or {}
        surf = surfaces.get(expiry)
        if surf is None:
            return None
        side_params = getattr(surf, "_side_params", None) or {}
        vals: List[float] = []
        for side, sp in side_params.items():
            # Prefer SVI result; fall back to SABR result.
            r = sp.get("svi_params") or sp.get("params")
            if r is None:
                continue
            rmse = getattr(r, "rmse", None)
            if rmse is not None:
                vals.append(float(rmse))
        if not vals:
            return None
        return max(vals)
