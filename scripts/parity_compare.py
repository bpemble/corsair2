#!/usr/bin/env python3
"""Parity comparison: trader decisions vs broker actuals.

Joins ``logs-paper/trader_decisions-YYYY-MM-DD.jsonl`` (the trader's
intended quotes at every tick) against the broker's ground-truth state
(latest resting orders from ``data/hg_chain_snapshot.json`` plus
order_lifecycle events for time-window analysis).

What this measures
------------------
For each (strike, expiry, right, side), we ask two questions:

  1. **Action match**: did the trader's latest "place" / "skip" decision
     agree with whether the broker has an order resting at that key?
  2. **Price match** (when both placed): is the trader's intended price
     within ``--price-tol-ticks`` of the broker's resting price?

Output is a per-key drift table plus an aggregate summary. The script
is read-only; safe to run any time.

Why this is the gate to Phase 3
-------------------------------
Until trader's actions match broker's actuals at ≥99%, cutting over the
trader to actually placeOrder would diverge from current behavior. The
remaining drift bins tell us which guards/edge-cases the trader's v2
``decide_quote`` is missing.

Usage:
    python3 scripts/parity_compare.py [--date YYYY-MM-DD]
                                      [--price-tol-ticks 1]
                                      [--show-drift-keys 20]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date as date_cls

DEFAULT_LOGS_DIR = "/home/ethereal/corsair/logs-paper"
DEFAULT_SNAPSHOT = "/home/ethereal/corsair/data/hg_chain_snapshot.json"
DEFAULT_TICK_SIZE = 0.0005


def _normalize_strike_key(s) -> str:
    """Snapshot uses string strike keys ("6", "6.05"); decisions use floats.
    Normalize both to canonical 4-decimal float-as-string."""
    return f"{float(s):.4f}"


def _normalize_expiry(e) -> str:
    return str(e).strip()


def load_latest_trader_decisions(path: str) -> dict:
    """Walk trader_decisions JSONL and keep only the LATEST decision per
    (strike, expiry, right, side). Returns dict[key] -> decision."""
    if not os.path.exists(path):
        print(f"trader decisions not found: {path}", file=sys.stderr)
        return {}
    latest: dict = {}
    with open(path) as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = row.get("decision") or {}
            strike = d.get("strike")
            expiry = d.get("expiry")
            right = d.get("right")
            side = d.get("side")
            if strike is None or expiry is None or right is None or side is None:
                continue
            key = (_normalize_strike_key(strike), _normalize_expiry(expiry), right, side)
            recv_ns = row.get("recv_ns", 0)
            cur = latest.get(key)
            if cur is None or recv_ns > cur["_recv_ns"]:
                latest[key] = {
                    "_recv_ns": recv_ns,
                    "action": d.get("action"),
                    "price": d.get("price"),
                    "theo": d.get("theo"),
                    "iv": d.get("iv"),
                    "reason": d.get("reason"),
                    "forward": row.get("forward"),
                }
    return latest


def load_broker_resting(snapshot_path: str) -> dict:
    """Extract broker's currently-resting orders from the chain snapshot.

    Returns dict[(strike, expiry, right, side)] -> {price, qty, status}.
    Empty/non-existent keys mean broker has no order resting there
    right now.
    """
    if not os.path.exists(snapshot_path):
        print(f"snapshot not found: {snapshot_path}", file=sys.stderr)
        return {}
    s = json.load(open(snapshot_path))
    out: dict = {}
    chains = s.get("chains") or {}
    for expiry, exp_block in chains.items():
        strikes = (exp_block or {}).get("strikes") or {}
        for strike_key, strike_block in strikes.items():
            for right_field, right in (("call", "C"), ("put", "P")):
                side_block = (strike_block or {}).get(right_field)
                if not side_block:
                    continue
                # The dashboard exposes our_bid / our_ask separately;
                # those are the resting prices we care about.
                bid = side_block.get("our_bid")
                ask = side_block.get("our_ask")
                k_norm = _normalize_strike_key(strike_key)
                if bid is not None:
                    out[(k_norm, _normalize_expiry(expiry), right, "BUY")] = {
                        "price": float(bid),
                    }
                if ask is not None:
                    out[(k_norm, _normalize_expiry(expiry), right, "SELL")] = {
                        "price": float(ask),
                    }
    return out


def compare(trader_latest: dict, broker_resting: dict,
            price_tol: float) -> dict:
    """Bucket each (strike, expiry, right, side) into one of:

    - both_place_match    : trader place ≈ broker resting price
    - both_place_drift    : trader place but |Δ| > tol vs broker price
    - both_idle           : trader skip and broker not resting
    - trader_place_only   : trader place but broker not resting (over-aggressive)
    - broker_place_only   : trader skip but broker resting (over-conservative)
    - trader_only_seen    : key never seen on broker side (e.g. trader saw a
                            tick for a strike outside broker's quote range)

    Returns ``{"summary": Counter, "details": dict}`` where details maps
    bucket -> list of (key, trader, broker) triples.
    """
    keys = set(trader_latest) | set(broker_resting)
    summary: Counter = Counter()
    details: dict[str, list] = defaultdict(list)
    price_diffs_ticks: list[float] = []

    for key in sorted(keys):
        t = trader_latest.get(key)
        b = broker_resting.get(key)

        if t is None:
            # Broker has a resting order at a key the trader never decided on.
            # This happens for stale orders or keys outside trader's tick stream.
            summary["broker_only_seen"] += 1
            details["broker_only_seen"].append((key, t, b))
            continue

        action = t.get("action")
        if action == "place":
            if b is None:
                summary["trader_place_only"] += 1
                details["trader_place_only"].append((key, t, b))
            else:
                # Both placed; compare prices.
                t_px = t.get("price")
                b_px = b.get("price")
                if t_px is None or b_px is None:
                    summary["both_place_partial"] += 1
                    details["both_place_partial"].append((key, t, b))
                else:
                    diff = abs(float(t_px) - float(b_px))
                    price_diffs_ticks.append(diff / DEFAULT_TICK_SIZE)
                    if diff <= price_tol:
                        summary["both_place_match"] += 1
                    else:
                        summary["both_place_drift"] += 1
                        details["both_place_drift"].append((key, t, b))
        elif action == "skip":
            if b is None:
                summary["both_idle"] += 1
            else:
                summary["broker_place_only"] += 1
                details["broker_place_only"].append((key, t, b))
        else:
            summary["trader_unknown_action"] += 1
            details["trader_unknown_action"].append((key, t, b))

    return {"summary": summary, "details": details,
            "price_diffs_ticks": price_diffs_ticks}


def fmt_decision(d: dict | None) -> str:
    if d is None:
        return "(none)"
    px = d.get("price")
    px_s = f"px={px:.4f}" if px is not None else "px=  -  "
    return f"{d.get('action','?'):5} {px_s} reason={d.get('reason','-')}"


def fmt_resting(b: dict | None) -> str:
    if b is None:
        return "(no order)"
    px = b.get("price")
    return f"resting={px:.4f}" if px is not None else "resting=  -  "


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", default=date_cls.today().strftime("%Y-%m-%d"),
                   help="Date stamp for the trader_decisions JSONL")
    p.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR)
    p.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    p.add_argument("--price-tol-ticks", type=int, default=1,
                   help="Price match tolerance, in ticks (default 1)")
    p.add_argument("--show-drift-keys", type=int, default=20,
                   help="How many drift rows to display (per bucket)")
    args = p.parse_args()

    decisions_path = os.path.join(args.logs_dir,
                                  f"trader_decisions-{args.date}.jsonl")
    trader = load_latest_trader_decisions(decisions_path)
    broker = load_broker_resting(args.snapshot)

    if not trader:
        print(f"No trader decisions found at {decisions_path}",
              file=sys.stderr)
        return 1
    if not broker:
        print(f"No broker resting orders in snapshot {args.snapshot} — "
              f"is broker quoting?", file=sys.stderr)

    price_tol = args.price_tol_ticks * DEFAULT_TICK_SIZE
    result = compare(trader, broker, price_tol)
    summary = result["summary"]
    details = result["details"]

    total = sum(summary.values())
    matches = summary["both_place_match"] + summary["both_idle"]
    drifts = total - matches - summary.get("broker_only_seen", 0)
    rate = (matches / max(1, matches + drifts)) * 100.0

    print(f"Parity report — date={args.date}, price_tol={args.price_tol_ticks} tick(s)")
    print(f"  trader decisions seen (latest per key): {len(trader)}")
    print(f"  broker resting orders right now:        {len(broker)}")
    print()
    print(f"{'bucket':<22} {'count':>6}")
    print("-" * 32)
    for bucket in (
        "both_place_match",
        "both_idle",
        "both_place_drift",
        "both_place_partial",
        "trader_place_only",
        "broker_place_only",
        "broker_only_seen",
        "trader_unknown_action",
    ):
        n = summary.get(bucket, 0)
        if n:
            print(f"{bucket:<22} {n:>6}")
    print()
    print(f"Action-match rate (excluding broker_only_seen): {rate:.1f}%  "
          f"({matches} / {matches + drifts})")

    diffs = result["price_diffs_ticks"]
    if diffs:
        diffs.sort()
        n = len(diffs)
        print(f"\nPrice diff (ticks) when both placed (n={n}):")
        print(f"  min={diffs[0]:.1f}  p50={diffs[n//2]:.1f}  "
              f"p90={diffs[int(n*0.9)]:.1f}  p99={diffs[int(n*0.99)]:.1f}  "
              f"max={diffs[-1]:.1f}")

    # Show top drift rows by bucket so the operator sees specific cases.
    for bucket in ("both_place_drift", "trader_place_only",
                   "broker_place_only"):
        rows = details.get(bucket, [])
        if not rows:
            continue
        print(f"\n=== Sample {bucket} (up to {args.show_drift_keys}) ===")
        for key, t, b in rows[: args.show_drift_keys]:
            strike, expiry, right, side = key
            print(f"  {expiry} {strike}{right} {side:4} | trader: "
                  f"{fmt_decision(t)} | broker: {fmt_resting(b)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
