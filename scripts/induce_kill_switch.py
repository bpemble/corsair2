#!/usr/bin/env python3
"""Induce a kill switch for Gate 0 validation (v1.4 §9.4).

Each v1.4 Tier-1 kill switch MUST be induced-breach tested on paper
before Stage 1 launch. This script writes a sentinel file that the
running corsair process picks up on the next risk check and fires the
corresponding kill through its REAL firing path:
  - cancel all resting orders
  - flatten options (for flatten-type kills)
  - flatten hedge (for flatten-type kills)
  - force hedge to 0 (for delta kill)
  - log kill_switch.jsonl event

Switches:
  daily_pnl  — Daily P&L halt (flatten, source=induced_daily_halt)
  margin     — SPAN margin kill (flatten, source=induced_risk)
  delta      — Delta kill       (hedge_flat, source=induced_risk)
  theta      — Theta halt       (halt, source=induced_risk)
  vega       — Vega halt        (halt, source=induced_risk)

After firing, verify:
  1. ``docker compose logs corsair`` shows KILL SWITCH ACTIVATED line
  2. ``logs-paper/kill_switch-YYYY-MM-DD.jsonl`` contains the event
     with source prefixed "induced_"
  3. ``risk.killed == True`` in the dashboard / snapshot
  4. For flatten-type kills, verify resting orders = 0 and options
     positions = 0 in ``ib.positions()``
  5. Clear the kill for re-testing by restarting corsair OR
     (for daily_halt) waiting for the next CME session rollover
     (17:00 CT).

Non-daily_halt induced kills are STICKY and require a corsair restart
to clear — that's intentional; the operator must manually confirm the
test succeeded before quoting resumes.

Usage:
    python scripts/induce_kill_switch.py --switch daily_pnl
    python scripts/induce_kill_switch.py --switch margin
    python scripts/induce_kill_switch.py --switch delta
    python scripts/induce_kill_switch.py --switch theta
    python scripts/induce_kill_switch.py --switch vega

    # inside the corsair container (from a `docker compose exec` shell):
    python scripts/induce_kill_switch.py --switch daily_pnl

    # from outside the container (sentinel must land on the VOLUME
    # shared with corsair — currently only /tmp is inside the container,
    # so run via exec):
    docker compose exec corsair python scripts/induce_kill_switch.py \\
        --switch daily_pnl
"""

import argparse
import os
import sys


SENTINELS = {
    "daily_pnl": "corsair_induce_daily_pnl",
    "margin":    "corsair_induce_margin",
    "delta":     "corsair_induce_delta",
    "theta":     "corsair_induce_theta",
    "vega":      "corsair_induce_vega",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Induce a v1.4 kill switch for Gate 0 validation.",
    )
    parser.add_argument(
        "--switch", required=True, choices=sorted(SENTINELS.keys()),
        help="Which kill switch to induce.")
    parser.add_argument(
        "--dir",
        default=os.environ.get("CORSAIR_INDUCE_DIR", "/tmp"),
        help="Directory to write the sentinel. Must match the dir used by "
             "risk_monitor (INDUCE_SENTINEL_DIR, also env-configurable via "
             "CORSAIR_INDUCE_DIR). Default: /tmp (works inside the corsair "
             "container via `docker compose exec`; for host-side invocation, "
             "set CORSAIR_INDUCE_DIR to a docker-mounted path).")
    args = parser.parse_args()

    path = os.path.join(args.dir, SENTINELS[args.switch])
    try:
        with open(path, "w") as f:
            f.write("induced\n")
    except OSError as e:
        print(f"ERROR: failed to write sentinel {path}: {e}", file=sys.stderr)
        return 1

    print(f"Sentinel written: {path}")
    print(f"Watch for:")
    print(f"  - 'KILL SWITCH ACTIVATED [induced_*] ...' in corsair logs")
    print(f"  - logs-paper/kill_switch-<today>.jsonl row with "
          f"source starting 'induced_'")
    print(f"  - Dashboard 'Kill' indicator flipping to red")
    print(f"The sentinel auto-deletes after firing on the next risk cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
