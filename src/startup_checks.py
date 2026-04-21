"""Boot-time self-test for kill-switch paths (v1.4 §9.4).

Runs each induced sentinel through a fresh RiskMonitor with mock
flatten/hedge callbacks and verifies:
  (a) the kill fires with the expected source and kill_type
  (b) the sentinel was removed before the kill fired (confirms our
      "remove-before-fire" guard prevents stuck re-triggers)
  (c) a kill_switch.jsonl row was appended for the event
  (d) daily_halt auto-clears via clear_daily_halt(); non-halt sources
      stay sticky (clear_daily_halt returns False)
  (e) flatten_callback / hedge_manager.force_flat were invoked for the
      matching kill_type

Rationale: the §9.4 sentinel mechanism exists but has required manual
runs. One missed sentinel test post-refactor leaves a kill armed-but-
disabled until a genuine breach fails to fire. Running all five at
boot and gating live orders on pass closes the silent-disable risk
surface for the kill-switch plumbing.

Scope note: this harness exercises the sentinel→kill→log plumbing. It
does NOT exercise the upstream detection paths (SABR RMSE sampling,
place-RTT sampling, fill-rate sampling). Those are covered by unit
tests and by the CRITICAL log emitted at RiskMonitor.__init__ when
daily_halt_threshold is None.
"""

import logging
import os
import time
from datetime import date

from .risk_monitor import (
    INDUCE_SENTINEL_DIR,
    INDUCE_SENTINELS,
    RiskMonitor,
)

logger = logging.getLogger(__name__)

_JSONL_FLUSH_WAIT_SEC = 2.0
_BYPASS_ENV_VAR = "CORSAIR_SKIP_KILL_SELF_TEST"


def _kill_switch_jsonl_path(csv_logger) -> str:
    return os.path.join(
        csv_logger._paper_log_dir,
        f"kill_switch-{date.today().isoformat()}.jsonl",
    )


def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def _wait_for_line_growth(path: str, baseline: int, timeout: float) -> bool:
    """Poll for the jsonl file to grow by at least one line. CSVLogger
    flushes in batches off a background thread, so we poll for up to
    ``timeout`` seconds before giving up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count_lines(path) > baseline:
            return True
        time.sleep(0.05)
    return False


class _MockHedge:
    """Minimal hedge stand-in. Records calls so assertions can verify
    invocation; no real futures orders ever hit the wire regardless of
    the production hedge mode."""

    def __init__(self):
        self.force_flat_called: list = []
        self.flatten_on_halt_called: list = []

    def force_flat(self, reason: str) -> None:
        self.force_flat_called.append(reason)

    def flatten_on_halt(self, reason: str) -> None:
        self.flatten_on_halt_called.append(reason)

    def mtm_usd(self) -> float:
        return 0.0


def run_kill_switch_self_test(
    portfolio, margin_checker, quote_manager, csv_logger, config,
) -> bool:
    """Exercise every induced sentinel against a fresh RiskMonitor and
    assert kill plumbing works end-to-end. Mock flatten/hedge callbacks
    ensure real positions/hedges are never touched.

    Returns True if all sentinels pass. On failure, logs CRITICAL and
    returns False — main.py should abort boot.

    Bypass: set CORSAIR_SKIP_KILL_SELF_TEST=1 in the environment to
    skip and return True. Intended for emergency recovery only; the
    bypass itself logs CRITICAL so operators can't miss it.
    """
    if os.environ.get(_BYPASS_ENV_VAR, "").strip() == "1":
        logger.critical(
            "KILL-SWITCH SELF-TEST BYPASSED via %s=1. This disables a "
            "safety-net check. Intended for emergency recovery only — "
            "unset before resuming normal operations.",
            _BYPASS_ENV_VAR,
        )
        return True

    logger.info(
        "Running boot-time kill-switch self-test (%d sentinels)...",
        len(INDUCE_SENTINELS),
    )

    mock_flatten_calls: list = []

    def _mock_flatten(reason: str) -> None:
        mock_flatten_calls.append(reason)

    jsonl_path = _kill_switch_jsonl_path(csv_logger)
    results: dict = {}

    for switch_key, (fname, kill_type, source) in INDUCE_SENTINELS.items():
        mock_hedge = _MockHedge()
        test_risk = RiskMonitor(
            portfolio, margin_checker, quote_manager, csv_logger, config,
            flatten_callback=_mock_flatten,
            hedge_manager=mock_hedge,
        )
        mock_flatten_calls.clear()

        sentinel_path = os.path.join(INDUCE_SENTINEL_DIR, fname)
        try:
            with open(sentinel_path, "w") as f:
                f.write("boot_self_test")
        except OSError as e:
            logger.error("self-test [%s]: sentinel write failed: %s",
                         switch_key, e)
            results[switch_key] = False
            continue

        baseline_lines = _count_lines(jsonl_path)

        fired = test_risk._check_induced_sentinels()

        expected_source = f"induced_{source}"
        ok_fired = (
            fired
            and test_risk.killed
            and test_risk._kill_source == expected_source
            and test_risk._kill_type == kill_type
        )

        ok_sentinel_removed = not os.path.exists(sentinel_path)
        if not ok_sentinel_removed:
            try:
                os.remove(sentinel_path)
            except OSError:
                pass

        ok_jsonl = _wait_for_line_growth(
            jsonl_path, baseline_lines, _JSONL_FLUSH_WAIT_SEC,
        )

        if source == "daily_halt":
            ok_clear = test_risk.clear_daily_halt() and not test_risk.killed
        else:
            ok_clear = (
                not test_risk.clear_daily_halt()
                and test_risk.killed
            )

        if kill_type == "flatten":
            ok_callback = bool(mock_flatten_calls)
        elif kill_type == "hedge_flat":
            ok_callback = bool(mock_hedge.force_flat_called)
        else:  # "halt"
            ok_callback = (
                not mock_flatten_calls
                and not mock_hedge.force_flat_called
            )

        passed = all([ok_fired, ok_sentinel_removed, ok_jsonl,
                      ok_clear, ok_callback])

        if passed:
            logger.info("self-test [%s] PASS", switch_key)
        else:
            logger.error(
                "self-test [%s] FAIL: fired=%s sentinel_removed=%s "
                "jsonl_grew=%s auto_clear=%s callback=%s (expected "
                "source=%s kill_type=%s; observed source=%s kill_type=%s)",
                switch_key, ok_fired, ok_sentinel_removed, ok_jsonl,
                ok_clear, ok_callback, expected_source, kill_type,
                test_risk._kill_source, test_risk._kill_type,
            )

        results[switch_key] = passed

    # Final cleanup: remove any lingering sentinel files.
    for switch_key, (fname, _, _) in INDUCE_SENTINELS.items():
        p = os.path.join(INDUCE_SENTINEL_DIR, fname)
        if os.path.exists(p):
            try:
                os.remove(p)
                logger.warning("self-test cleanup: removed stray sentinel %s", p)
            except OSError as e:
                logger.error(
                    "self-test cleanup: could not remove stray sentinel %s: %s",
                    p, e,
                )

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    if passed == total:
        logger.info(
            "KILL-SWITCH SELF-TEST PASSED: %d/%d sentinels verified "
            "(fire + sentinel-removal + jsonl + auto-clear + callback)",
            passed, total,
        )
        return True
    failed = sorted(k for k, v in results.items() if not v)
    logger.critical(
        "KILL-SWITCH SELF-TEST FAILED: %d/%d passed. Failed: %s. "
        "Refusing to accept live orders. Fix the kill path or set %s=1 "
        "to bypass (NOT recommended).",
        passed, total, failed, _BYPASS_ENV_VAR,
    )
    return False
