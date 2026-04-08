"""Standalone synthetic probe for IBKR amend latency.

Bypasses corsair entirely. Connects with the lean clientId=0 bootstrap,
places a single deeply-OTM resting limit order (won't fill), modifies its
limit price N times, and measures each (place→Submitted) and
(modify→canonical-reflects-new-price) latency independently.

Goal: isolate whether the 461ms p90 amend latency we observe in production
is paper-Gateway latency (in which case nothing client-side helps) or a
corsair-side artifact (in which case we tune our own pipeline).

Run via:
    docker compose run --rm -v ~/corsair2/scripts:/app/scripts corsair \
        python3 /app/scripts/amend_latency_probe.py

REQUIRES corsair to be stopped (clientId=0 conflict). Stop with:
    docker compose stop corsair
"""
import asyncio
import os
import statistics
import sys
import time

from ib_insync import IB, FuturesOption, LimitOrder

HOST = os.environ.get("CORSAIR_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORSAIR_GATEWAY_PORT", "4002"))
ACCOUNT = os.environ.get("CORSAIR_ACCOUNT_ID") or os.environ.get("IBKR_ACCOUNT")
if not ACCOUNT:
    print("ERROR: CORSAIR_ACCOUNT_ID not set")
    sys.exit(1)

# Probe parameters
EXPIRY = "20260424"      # ETHJ6
STRIKE = 1500.0          # deeply OTM put — won't fill, easy to cancel
RIGHT = "P"
N_AMENDS = 30            # number of modify cycles
AMEND_DELAY_S = 0.5      # gap between modifies
START_PRICE = 1.50       # arbitrary, far below any realistic bid
PRICE_STEP = 0.50        # one tick

print(f"Probe: {RIGHT}{int(STRIKE)} {EXPIRY}, {N_AMENDS} amends @ {AMEND_DELAY_S}s gap")
print(f"Connecting to {HOST}:{PORT} as clientId=0, account={ACCOUNT}")

ib = IB()


async def lean_connect():
    await ib.client.connectAsync(HOST, PORT, 0, 30)
    ib.reqAutoOpenOrders(True)
    reqs = [
        ib.reqPositionsAsync(),
        ib.reqOpenOrdersAsync(),
        ib.reqAccountUpdatesAsync(ACCOUNT),
    ]
    await asyncio.gather(
        *(asyncio.wait_for(c, 20) for c in reqs),
        return_exceptions=True,
    )


async def probe():
    await lean_connect()
    print(f"Connected. Server version {ib.client.serverVersion()}")

    # Qualify the contract
    contract = FuturesOption("ETHUSDRR", EXPIRY, STRIKE, RIGHT, "CME", "50")
    await ib.qualifyContractsAsync(contract)
    if not contract.conId:
        print(f"ERROR: failed to qualify {RIGHT}{int(STRIKE)} {EXPIRY}")
        ib.disconnect()
        return
    print(f"Qualified: {contract.localSymbol} conId={contract.conId}")

    # ── Place ────────────────────────────────────────────────────────
    order = LimitOrder(
        action="BUY",
        totalQuantity=1,
        lmtPrice=START_PRICE,
        tif="DAY",
        account=ACCOUNT,
        orderRef="amend_probe_initial",
    )
    place_t0 = time.monotonic_ns()
    trade = ib.placeOrder(contract, order)
    print(f"Placed orderId={trade.order.orderId} @ {START_PRICE}")

    # Wait for canonical Trade to advance past PendingSubmit. We poll
    # openTrades() rather than relying on trade.statusEvent because the
    # local Trade reference may be the orphan (see CLAUDE.md note 2).
    target_oid = trade.order.orderId

    def canonical():
        latest = None
        for t in ib.openTrades():
            if t.order.orderId == target_oid:
                latest = t
        return latest

    place_rtt_us = None
    deadline = time.monotonic() + 10  # 10s budget
    while time.monotonic() < deadline:
        c = canonical()
        if c is not None and c.orderStatus.status not in ("PendingSubmit", "ApiPending", ""):
            place_rtt_us = (time.monotonic_ns() - place_t0) // 1000
            break
        await asyncio.sleep(0.005)  # 5ms polling

    if place_rtt_us is None:
        print("ERROR: order never advanced past PendingSubmit within 10s")
        ib.cancelOrder(trade.order)
        ib.disconnect()
        return

    print(f"Place RTT: {place_rtt_us/1000:.1f}ms ({canonical().orderStatus.status})")

    # ── Modify N times ────────────────────────────────────────────────
    amend_us_samples = []
    for i in range(N_AMENDS):
        await asyncio.sleep(AMEND_DELAY_S)
        new_price = START_PRICE + (i + 1) * PRICE_STEP
        # Get the canonical trade object (its .order is what placeOrder
        # treats as a modify when it sees the same orderId)
        c = canonical()
        if c is None:
            print(f"  amend {i}: lost canonical trade, abort")
            break
        c.order.lmtPrice = new_price
        modify_t0 = time.monotonic_ns()
        ib.placeOrder(c.contract, c.order)

        # Wait for canonical lmtPrice to reflect the new value
        amend_us = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            c2 = canonical()
            if c2 is not None and abs(c2.order.lmtPrice - new_price) < 1e-9:
                amend_us = (time.monotonic_ns() - modify_t0) // 1000
                break
            await asyncio.sleep(0.005)

        if amend_us is None:
            print(f"  amend {i+1}: timed out @ {new_price}")
        else:
            amend_us_samples.append(amend_us)
            print(f"  amend {i+1:2d}: {amend_us/1000:6.1f}ms  @ {new_price}")

    # ── Cleanup: cancel the resting order ────────────────────────────
    c = canonical()
    if c is not None:
        ib.cancelOrder(c.order)
        await asyncio.sleep(1)

    ib.disconnect()

    # ── Report ────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"Place RTT (1 sample): {place_rtt_us/1000:.1f}ms")
    if amend_us_samples:
        n = len(amend_us_samples)
        s = sorted(amend_us_samples)
        p50 = s[n // 2]
        p90 = s[min(n - 1, int(n * 0.90))]
        p99 = s[min(n - 1, int(n * 0.99))]
        mean = statistics.mean(amend_us_samples)
        print(f"Amend RTT (n={n}):")
        print(f"  mean = {mean/1000:.1f}ms")
        print(f"  p50  = {p50/1000:.1f}ms")
        print(f"  p90  = {p90/1000:.1f}ms")
        print(f"  p99  = {p99/1000:.1f}ms")
        print(f"  min  = {s[0]/1000:.1f}ms")
        print(f"  max  = {s[-1]/1000:.1f}ms")


ib.run(probe())
