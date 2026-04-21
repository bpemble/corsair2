"""Read-only IBKR position + execution reconcile against corsair's view.

Connects on a SEPARATE clientId (99) so it can't disturb corsair's
master-client (clientId=0) traffic. Compares:
  - ib.positions() / ib.reqPositionsAsync() against chain_snapshot.json
    portfolio.positions
  - ib.reqExecutions() (today) against chain_snapshot.json fills_today

Run from inside the corsair docker network so it can hit ib-gateway
at the same host:port corsair uses.

Usage:
    docker compose run --rm --no-deps \\
        -v $(pwd)/scripts:/app/scripts \\
        -v $(pwd)/data:/app/data \\
        corsair python -m scripts.reconcile_positions
"""

import asyncio
import json
import sys
from collections import defaultdict

from ib_insync import IB, ExecutionFilter

GATEWAY_HOST = "127.0.0.1"  # host networking — same as corsair
GATEWAY_PORT = 4002
CLIENT_ID = 99
ACCOUNT = "DUP553657"  # matches IBKR_ACCOUNT in .env
SNAPSHOT = "data/chain_snapshot.json"


async def main() -> int:
    ib = IB()
    print(f"Connecting to {GATEWAY_HOST}:{GATEWAY_PORT} as clientId={CLIENT_ID}...")
    await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=CLIENT_ID, timeout=20)
    print(f"Connected. Server version: {ib.client.serverVersion()}")

    # ── Positions from IBKR ─────────────────────────────────────────
    positions = await ib.reqPositionsAsync()
    eth_opts = [
        p for p in positions
        if p.account == ACCOUNT and p.contract.symbol == "ETH"
        and p.contract.secType == "FOP" and p.position != 0
    ]
    print(f"\n=== IBKR positions (account {ACCOUNT}, ETH FOP, non-zero) ===")
    print(f"count: {len(eth_opts)}")
    ibkr_by_key = {}
    for p in eth_opts:
        key = (p.contract.strike, p.contract.right, p.contract.lastTradeDateOrContractMonth)
        ibkr_by_key[key] = p.position
        print(f"  {int(p.contract.strike)}{p.contract.right} {p.contract.lastTradeDateOrContractMonth}: qty={p.position} avg={p.avgCost:.2f}")

    # ── Today's executions from IBKR ────────────────────────────────
    fills = await ib.reqExecutionsAsync(ExecutionFilter(acctCode=ACCOUNT))
    eth_fills = [f for f in fills if f.contract.symbol == "ETH" and f.contract.secType == "FOP"]
    print(f"\n=== IBKR executions today (ETH FOP) ===")
    print(f"count: {len(eth_fills)}")
    fill_summary = defaultdict(lambda: {"buy": 0, "sell": 0})
    for f in eth_fills:
        side_key = "buy" if f.execution.side == "BOT" else "sell"
        key = (f.contract.strike, f.contract.right)
        fill_summary[key][side_key] += f.execution.shares
    for (strike, right), v in sorted(fill_summary.items()):
        print(f"  {int(strike)}{right}: bought={v['buy']} sold={v['sell']} net={v['buy']-v['sell']}")

    # ── Compare against corsair's snapshot ──────────────────────────
    try:
        snap = json.load(open(SNAPSHOT))
    except Exception as e:
        print(f"\nCould not load {SNAPSHOT}: {e}")
        ib.disconnect()
        return 1

    snap_positions = snap.get("portfolio", {}).get("positions", [])
    snap_by_key = {}
    for p in snap_positions:
        snap_by_key[(float(p["strike"]), p["right"], p["expiry"])] = p["qty"]

    print(f"\n=== Corsair snapshot positions ===")
    print(f"count: {len(snap_positions)}")
    print(f"fills_today: {snap.get('portfolio', {}).get('fills_today')}")
    for k, q in sorted(snap_by_key.items()):
        print(f"  {int(k[0])}{k[1]} {k[2]}: qty={q}")

    # ── Diff ────────────────────────────────────────────────────────
    print(f"\n=== DIFF (IBKR vs corsair) ===")
    all_keys = set(ibkr_by_key.keys()) | set(snap_by_key.keys())
    diffs = 0
    for key in sorted(all_keys):
        ibkr_qty = ibkr_by_key.get(key, 0)
        corsair_qty = snap_by_key.get(key, 0)
        if ibkr_qty != corsair_qty:
            diffs += 1
            print(f"  MISMATCH {int(key[0])}{key[1]} {key[2]}: IBKR={ibkr_qty} corsair={corsair_qty} delta={ibkr_qty-corsair_qty}")
    if diffs == 0:
        print("  positions match exactly")
    else:
        print(f"\n  *** {diffs} mismatches — corsair view of book is WRONG ***")

    # Also check executions vs fills_today
    ibkr_total_fills = sum(v["buy"] + v["sell"] for v in fill_summary.values())
    print(f"\n  IBKR exec count today: {ibkr_total_fills}")
    print(f"  corsair fills_today:   {snap.get('portfolio', {}).get('fills_today')}")
    if ibkr_total_fills != snap.get("portfolio", {}).get("fills_today", 0):
        print("  *** fills_today mismatch — fill_handler is dropping events ***")

    ib.disconnect()
    return 0 if diffs == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
