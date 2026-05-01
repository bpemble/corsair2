# MM Service Split — Architecture Design

**Status:** Draft, pre-implementation
**Author context:** Designed 2026-04-30 from a Corsair operator session. Goal:
prepare the architecture for a future colocated FIX-direct execution
venue, while extracting tick-side latency wins on the current IBKR
paper system.

## 1. Goal

Move the tick → decision → placeOrder hot path off the asyncio loop
that today shares cycles with snapshot writing, SABR refit dispatch,
hedge management, position reconcile, op-kills, and the watchdog. TTT
p99 spikes correlate with synchronous handlers in the slow set
blocking the loop; isolating the hot path eliminates those.

This is also colo prep: when the colo system replaces the IBKR
gateway with FIX-direct, the tick-path process should be relocatable
with minimum churn — same process boundary, different transport.

## 2. Constraint: single IBKR clientId

Operator constraint (2026-04-30): the live system uses a single
clientId. We cannot run two ib_insync `IB()` instances against the
gateway.

This rules out the architecturally cleanest option (two independent
gateway connections, one per process) and forces a **broker pattern**:
exactly one process owns the connection, and any other process that
needs gateway I/O goes through it.

## 3. Two-process architecture

```
                        ┌──────────────────────────────────────┐
                        │  broker process (= today's corsair)  │
                        │                                      │
                        │  ─ owns gateway connection           │
                        │    (clientId=0, FA orderKey patch)   │
                        │  ─ asyncio loop runs ib_insync I/O   │
                        │    AND minimal forwarding only       │
                        │  ─ slow workloads (risk monitor,     │
                        │    snapshot writer, hedge manager,   │
                        │    op-kills, position reconcile,     │
                        │    watchdog)                         │
                        │                                      │
                        └─────┬────────────────────────────▲───┘
            tick events       │                            │
            order acks        │                            │ placeOrder /
            fill events       │                            │ cancelOrder
                              ▼                            │ requests
                        ┌─────────────────────────────────┴────┐
                        │  trader process (NEW)                │
                        │                                      │
                        │  ─ no gateway connection             │
                        │  ─ owns the hot path:                │
                        │      tick → SABR theo → quote        │
                        │      decision → placeOrder request   │
                        │  ─ owns local state:                 │
                        │      price book, active_orders,      │
                        │      canonical_idx                   │
                        │  ─ rust hot-path (already in place)  │
                        │                                      │
                        └──────────────────────────────────────┘
```

The broker keeps everything that today is "supervisor-like": risk,
snapshot, hedge, kill switches. Eventually some of those (hedge,
snapshot, risk) could move to their own processes, but the v1 split
is binary.

## 4. Hot-path latency budget

Today (single-loop): TTT p50 ~1.5ms, p99 ~5ms (steady) with rare
spikes from contending handlers.

Two-process target: p50 ~0.5–0.8ms, p99 ~1–2ms. The win comes from:

- **Broker loop is a pass-through** — its only synchronous workload is
  forwarding events to the trader and forwarding orders to gateway.
  Nothing else (snapshot build, SABR dispatch, hedge math) competes
  with tick delivery on its loop.
- **Trader loop has nothing else on it** — pure tick → quote
  evaluation. No 4Hz snapshot, no 30s hedge cycle.

IPC adds two SHM hops on the critical path (tick forward + order
forward back). Budget: ≤5μs round-trip for SHM-based IPC.

| Stage | Budget |
|---|---|
| Gateway TCP recv (loopback) → ib_insync parse | ~150-300μs |
| Broker forwards event to trader (SHM write) | ~1-2μs |
| Trader reads, runs decision (Rust pricing) | ~200-400μs |
| Trader writes placeOrder request (SHM write) | ~1-2μs |
| Broker reads request, calls ib.placeOrder | ~100-200μs |
| **Total tick → placeOrder** | **~450-900μs** |

Achievable; below 1ms median. Spikes still exist (GC, scheduler) but
no longer driven by snapshot/SABR contention.

## 5. IPC mechanism

V1: **Unix domain socket** for both data and control planes. Adds
~10-20μs per hop (syscall + small data). Simple, robust, debuggable.

V2 (post-Phase 4): **shared-memory ring buffers** (SPSC, lock-free) for
the hot path. ~1-2μs per hop. eventfd for wakeup so consumer doesn't
busy-loop. Implementation can mirror what the colo system will use,
making the eventual port near-trivial.

Why not start with SHM in V1: SHM rings are easy to get wrong (memory
ordering, partial writes on crash, etc.). Start with sockets, get the
abstraction right, then swap implementations behind it.

## 6. Protocol

Two channels: **events** (broker → trader) and **commands** (trader →
broker). Both length-prefixed message-pack frames over the same socket
or two unidirectional sockets.

### Broker → trader events

| Event | Payload | When |
|---|---|---|
| `tick` | `{strike, expiry, right, bid, ask, bid_size, ask_size, last, vol, oi, ts_ns}` | Per option ticker.updateEvent fire (after ib_insync batch) |
| `underlying_tick` | `{price, ts_ns}` | Per underlying ticker update |
| `order_ack` | `{orderId, status, filled, remaining, lmtPrice, ts_ns}` | Per orderStatusEvent fire |
| `fill` | `{orderId, side, qty, price, exec_id, contract_*, ts_ns}` | Per execDetailsEvent fire |
| `vol_surface_update` | `{expiry, params}` (SABR/SVI fitted params) | After each SABR fit |
| `kill` | `{reason, source, kill_type}` | When risk/op-kills fire |
| `resume` | `{}` | When kill clears (e.g. daily P&L halt at session rollover) |
| `weekend_pause` | `{paused: bool}` | Friday close / Sunday reopen |

### Trader → broker commands

| Command | Payload | Effect |
|---|---|---|
| `place_order` | `{strike, expiry, right, side, qty, price, orderRef, account, ts_ns}` | Broker calls ib.placeOrder |
| `cancel_order` | `{orderId, ts_ns}` | Broker calls ib.cancelOrder |
| `subscribe_chain` | `{strikes, expiries}` | Broker reqMktData for the listed contracts (idempotent) |
| `unsubscribe_chain` | `{strikes, expiries}` | Broker cancelMktData |
| `telemetry` | `{ttt_p50, ttt_p99, n_active, ts_ns}` | Broker logs / forwards to dashboard |

### State shared via local files (not IPC)

These don't change frequently and don't need low latency:

- **Vol surface**: SABR fit params written to `data/vol_surface.json` by
  the broker (which runs the SABR fit). Trader watches the file and
  reloads on change. Could be IPC instead — file is fine for v1.
- **Position book**: broker maintains positions, writes
  `data/positions.json` on update. Trader reads on startup; doesn't
  need it for the hot path.
- **Daily state**: `data/daily_state.json` already exists; same
  pattern.

## 7. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Trader crashes | Broker socket EOF → broker closes channel | Broker auto-cancels all open orders (panic_cancel), waits for trader restart, replays last-known state |
| Broker crashes | Trader socket EOF → trader stops sending orders | Trader can't recover; supervisor (systemd / docker compose) restarts broker, which then restarts trader |
| Gateway disconnects | Existing watchdog logic, lives in broker | Broker pauses event forwarding; trader queues nothing during outage; resume on reconnect |
| Trader → broker socket buffer fills (slow broker) | Trader sees `EWOULDBLOCK` | Drop oldest queued command, log, alert |
| Broker → trader socket buffer fills (slow trader) | Broker sees `EWOULDBLOCK` | Drop oldest non-critical event (ticks); never drop kill/resume/order_ack |

Critical invariant: **kill events MUST be delivered**. If the trader
isn't draining its socket fast enough, broker still has authority over
orders (it owns the gateway connection) and runs `panic_cancel`
locally.

## 8. Compatibility with colo migration

When colo lands:
- Replace broker with a Rust binary that talks FIX directly to CME
- Trader binary unchanged (or recompiled with a different IPC layer)
- Protocol stays the same — vol surface updates, orders, fills, kills
- Most reuse comes from getting the trader's hot path correct now

The trader's protocol is intentionally transport-agnostic. We're
designing for "give me ticks, take my orders" — same shape over IBKR
or FIX.

## 9. Phased delivery

| Phase | Deliverable | Risk |
|---|---|---|
| 0 | Design doc (this document), reviewed | None |
| 1 | IPC abstraction (`ipc.py`): event publisher + consumer over Unix socket. Stub trader that connects, prints received ticks. **Broker side OFF by default**, opt-in via env var. No production behavior change. | Low |
| 2 | Broker forwards real tick / fill / order_ack events. Trader runs SABR theo lookup and logs intended quote decisions to JSONL for parity comparison vs current corsair. **Trader does NOT place orders.** | Medium — broker has new hot-path code, but always-on event forwarding |
| 3 | Trader places orders via broker. Current corsair stops quoting (its update_quotes is gated off when trader is up). Compare TTT before/after. Rollback: re-enable corsair quoting, kill trader. | High — first cut-over |
| 4 | Move slow workloads (snapshot writer, SABR dispatch) off broker's loop into worker threads within the broker process. Broker's asyncio loop becomes a pure I/O routing loop. | Medium |
| 5 | Tighten IPC: SHM rings + eventfd for hot path. Order placement path can go to ~5μs IPC overhead. | Medium |
| 6 | (Optional) Move trader's pricing/decision into Rust as the colo path will need it anyway. | Low — extends existing PyO3 work |

Each phase is independently shippable and reversible.

## 10. Out of scope (intentionally)

- Multi-product trading (HG only today, ETH tabled)
- Dashboard rewrite — keeps reading the snapshot file the broker still
  writes, no behavioral change
- Strategy logic changes — same SABR/SVI/quote-selection logic, just
  hosted in a different process
- Anything touching the FA orphan-fix in `connection.py:_fa_orderKey` —
  broker keeps that exactly as-is

## 11. Open questions

1. **Does ib_insync expose enough for the broker to forward events
   efficiently?** Need to confirm `ticker.updateEvent`, `orderStatusEvent`,
   `execDetailsEvent`, `openOrderEvent` all expose the data we need
   without us touching wrapper.trades directly.

2. **Token bucket placement.** Today the rate limiter
   (`_try_place_order`) lives in the trader-side code. With the broker
   actually issuing orders, we likely want the bucket on the broker
   side so any future second trader (e.g. hedge process placing futures
   orders) shares the limit. Phase 3 design call.

3. **Hedge orders.** Hedge_manager today places futures orders directly.
   Easiest is: hedge stays in the broker, calls ib.placeOrder
   directly, no IPC needed. Cleanest split is: hedge is also a
   "trader" peer that talks to the broker. We can defer until phase 4
   to decide.

4. **Restart sequencing.** If broker restarts, trader needs to
   gracefully reconnect; orders placed during the outage may not be in
   trader's `active_orders`. State sync at handshake — broker forwards
   "current open orders" snapshot to trader on connect. Phase 3 design.

## 12. Phase 1 specifics (for the next session)

Files added:
- `src/ipc/__init__.py`
- `src/ipc/protocol.py` — message-pack framing, event/command enums
- `src/ipc/server.py` — broker-side event publisher
- `src/ipc/client.py` — trader-side consumer
- `src/trader/main.py` — stub: connect, print events, exit on signal
- `Dockerfile.trader` — new image for the trader container
- `docker-compose.yml` — adds `trader` service, depends_on broker

Files modified:
- `src/main.py` — opt-in event forwarding when
  `CORSAIR_BROKER_MODE=1`. Wraps existing event subscriptions to also
  publish to IPC. Default off → zero behavior change.
- `requirements.txt` — adds `msgpack`

Acceptance:
- Broker still works exactly as before with `CORSAIR_BROKER_MODE` unset
- With it set, trader process connects, prints first 100 tick events,
  measures IPC latency, exits cleanly
- No order placement on trader side
- Parity test still passes
- Unit tests for `protocol.py` framing
