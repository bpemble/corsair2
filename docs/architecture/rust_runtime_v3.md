# Corsair v3 — Rust Runtime Migration Spec

**Status**: PROPOSED — pending approval
**Author**: Claude (drafted 2026-05-02 with operator)
**Owner**: ethereal
**Supersedes**: piecewise Python→Rust ports (corsair_pricing, corsair_trader)
**Estimated effort**: 14-18 weeks of focused work, always shippable

## 1. Goals

1. **Production runtime is 100% Rust.** Every persistent service (broker, trader,
   risk, OMS) is a Rust binary. Python is not loaded in any process that holds
   capital, places orders, or watches a tick stream.
2. **Python is for offline work.** Research notebooks, backtesting, parameter
   sweeps, post-trade analysis, ops scripts, dashboard UI. Python writes
   configs/parameters; Rust consumes them.
3. **The exchange gateway is swappable.** IBKR today, FCM/iLink later. The
   gateway lives behind a single trait so swap = adapter rewrite, not a
   system rewrite.

## 2. Non-goals

- Re-architecting the strategy itself. v3 is structural; quoting / hedging /
  risk policy are unchanged.
- Reimplementing every Python tool. Streamlit dashboard stays Python.
  Research scripts stay Python. Ops utilities stay Python.
- Deploying to colo. Colo decisions follow capital/volume signals from
  paper trading, not from this migration.
- Changing the kill-switch taxonomy or v1.4 spec invariants.

## 3. End-state architecture

```
┌──────────────────────────── Production (Rust) ────────────────────────────┐
│                                                                            │
│   ┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐  │
│   │ corsair_broker  │ IPC │ corsair_trader   │     │  corsair_risk    │  │
│   │  daemon         │────▶│  decision logic  │     │  (kill switches, │  │
│   │  (this spec)    │ SHM │  (have)          │     │   sentinels)     │  │
│   └────────┬────────┘     └──────────────────┘     └──────────────────┘  │
│            │                                                ▲              │
│            │ Broker trait                                   │ in-proc      │
│            ▼                                                │              │
│   ┌─────────────────────────────────────────┐  ┌────────────────────────┐│
│   │ corsair_broker_ibkr  ←── swap point ──▶ │  │ corsair_position       ││
│   │ corsair_broker_ilink (future)           │  │ corsair_oms            ││
│   │   ──────                                 │  │ corsair_hedge          ││
│   │   IBKR API V100+ / FIX 4.2 / MDP3        │  │ corsair_constraint     ││
│   └─────────────────────────────────────────┘  │ corsair_market_data    ││
│                                                  │ corsair_snapshot       ││
│   ┌─────────────────────────────────────────┐   └────────────────────────┘│
│   │ corsair_pricing  (have)                 │                              │
│   │   Black-76, IV, SABR, SVI, calibrators, │                              │
│   │   Greeks, SPAN, decide_quote            │                              │
│   └─────────────────────────────────────────┘                              │
│                                                                            │
└────────┬───────────────────────────────────────────────────┬──────────────┘
         │                                                   │
         │ writes JSONL streams + snapshots                  │ reads configs
         ▼                                                   │
┌────────────────────────── Offline (Python) ────────────────────────────────┐
│                                                                            │
│   research/        — surface fitting research, regime studies              │
│   backtesting/     — replay JSONL streams, evaluate alt strategies         │
│   sweeps/          — parameter optimization (e.g. min_edge_ticks)         │
│   post_trade/      — fill quality, P&L attribution, adverse selection     │
│   ops/             — flatten, reconcile, induce_kill                       │
│   dashboard/       — Streamlit UI (read-only against snapshot files)       │
│                                                                            │
│   configs/runtime.yaml — runtime knobs, written by humans + research jobs  │
│   configs/params/      — model params (vol surfaces, calibration outputs)  │
└────────────────────────────────────────────────────────────────────────────┘
```

## 4. Boundary contract

The boundary between Rust runtime and Python tools is **one-directional and
asynchronous**. No synchronous Python→Rust call paths in production.

### 4.1 Python → Rust (configs and parameters)

**Channel**: file system. YAML/TOML configs at well-known paths. Rust
validates schema with `serde` + custom validators at startup; rejects
invalid configs (the runtime never silently accepts a malformed knob).

**Examples**:
- `config/runtime.yaml` — broker selection, risk thresholds, capital
- `config/params/sabr_seed.toml` — initial-guess sweeps from research
- `config/products/hg.yaml` — product-specific scope (strikes, expiries)

**Hot-reload**: NOT supported in v3. Config changes require a controlled
restart. Rationale: hot-reload adds drift risk between the live runtime
and what the config file says; restart is auditable.

**Sentinel files** (existing pattern, kept): `/tmp/corsair_induce_<switch>` for
induced-kill testing. These are operator-driven, not Python-driven.

### 4.2 Rust → Python (events and state)

**Channels**:
1. **JSONL streams** at `logs/*.jsonl`. Append-only, line-delimited.
   Already the production logging format (8 streams in v1.4 spec §9.5).
   Python research jobs read these directly with `pandas.read_json(lines=True)`.
2. **Snapshot file** at `data/snapshot.json`. Atomic-replaced at 4 Hz by
   `corsair_snapshot`. Streamlit dashboard polls this.
3. **Health endpoint** (Phase 5+): Unix domain socket exposing read-only
   inspection (`/state`, `/positions`, `/orders`). For ops tooling only.

**No RPCs**. Python tools never call into the Rust runtime to mutate state.
If you want to flatten positions, you write a sentinel file or use the
broker's own ops CLI; the runtime reacts asynchronously.

### 4.3 Why this boundary

- **Restart is the unit of change**: the runtime can be killed and restarted
  without a flood of pending RPCs. Configs are file-based; state is in IBKR
  (or FCM) and reconciled at boot.
- **Python toolchain decoupling**: research code can use any Python version,
  any library set, without affecting the runtime's wheel build.
- **Audit trail**: every config change is `git diff`-able. Every state
  change is in JSONL streams.

## 5. The Broker trait — the keystone

This is the contract the IBKR/iLink swap pivots on. Get this right and
swapping is a 4-6 week adapter project; get it wrong and it's an 8-12
week rewrite.

### 5.1 Trait definition (proposed)

```rust
// rust/corsair_broker_api/src/lib.rs

use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, Utc};
use futures::Stream;

#[async_trait]
pub trait Broker: Send + Sync + 'static {
    // ── Lifecycle ─────────────────────────────────────────────
    async fn connect(&mut self, cfg: &BrokerConfig) -> Result<(), BrokerError>;
    async fn disconnect(&mut self) -> Result<(), BrokerError>;
    fn is_connected(&self) -> bool;

    // ── Order entry ───────────────────────────────────────────
    async fn place_order(&self, req: PlaceOrderReq) -> Result<OrderId, BrokerError>;
    async fn cancel_order(&self, id: OrderId) -> Result<(), BrokerError>;
    async fn modify_order(&self, id: OrderId, req: ModifyOrderReq)
        -> Result<(), BrokerError>;

    // ── State queries ─────────────────────────────────────────
    async fn positions(&self) -> Result<Vec<Position>, BrokerError>;
    async fn account_values(&self) -> Result<AccountSnapshot, BrokerError>;
    async fn open_orders(&self) -> Result<Vec<OpenOrder>, BrokerError>;

    // ── Contract resolution ──────────────────────────────────
    async fn qualify_future(&self, q: FutureQuery) -> Result<Contract, BrokerError>;
    async fn qualify_option(&self, q: OptionQuery) -> Result<Contract, BrokerError>;
    async fn list_chain(&self, q: ChainQuery)
        -> Result<Vec<Contract>, BrokerError>;

    // ── Market data subscriptions ────────────────────────────
    async fn subscribe_ticks(&self, sub: TickSubscription)
        -> Result<TickStreamHandle, BrokerError>;
    async fn unsubscribe_ticks(&self, h: TickStreamHandle)
        -> Result<(), BrokerError>;

    // ── Push streams (broker → runtime) ──────────────────────
    fn fill_stream(&self) -> Box<dyn Stream<Item = Fill> + Send + Unpin>;
    fn order_status_stream(&self) -> Box<dyn Stream<Item = OrderStatusUpdate> + Send + Unpin>;
    fn tick_stream(&self) -> Box<dyn Stream<Item = Tick> + Send + Unpin>;
    fn error_stream(&self) -> Box<dyn Stream<Item = BrokerError> + Send + Unpin>;
    fn connection_stream(&self) -> Box<dyn Stream<Item = ConnectionEvent> + Send + Unpin>;

    // ── Capabilities (let consumers query without env-checking) ──
    fn capabilities(&self) -> &BrokerCapabilities;
}

pub struct BrokerCapabilities {
    /// True for IBKR FA accounts (every order needs `account=`)
    pub requires_account_per_order: bool,
    /// True if drop-copy / exec details arrive via a separate session
    /// (typical for FCM/iLink setups)
    pub fills_on_separate_channel: bool,
    /// Minimum order TIF the broker accepts. IOC for both today.
    pub supported_tifs: Vec<TimeInForce>,
    /// Wall-clock latency budget you can expect (median + p99).
    /// Used for setting GTD timeouts and watchdog thresholds.
    pub typical_rtt_ms: (u32, u32),
}
```

### 5.2 Value types (broker-agnostic)

```rust
// Identity
pub struct OrderId(pub u64);            // broker-assigned
pub struct InstrumentId(pub u64);        // broker-assigned (IBKR conId, iLink security_id)

// Contracts
pub struct Contract {
    pub instrument_id: InstrumentId,
    pub kind: ContractKind,             // Future | Option
    pub symbol: String,                 // root: "HG", "ETHUSDRR"
    pub local_symbol: String,           // "HGM6", "ETHUSDRR  260424P02100000"
    pub expiry: NaiveDate,
    pub strike: Option<f64>,            // None for futures
    pub right: Option<Right>,           // Call | Put | None for futures
    pub multiplier: f64,
    pub exchange: Exchange,             // CME | COMEX | etc.
    pub currency: Currency,
}

// Orders
pub struct PlaceOrderReq {
    pub contract: Contract,
    pub side: Side,                     // Buy | Sell
    pub qty: u32,
    pub order_type: OrderType,          // Limit | Market | StopLimit
    pub price: Option<f64>,
    pub tif: TimeInForce,               // Day | Gtd | Ioc | Fok
    pub gtd_until_utc: Option<DateTime<Utc>>,
    pub client_order_ref: String,       // our tracking key
    pub account: Option<String>,        // FA / sub-account selector
}

// Events
pub struct Fill {
    pub exec_id: String,
    pub order_id: OrderId,
    pub instrument_id: InstrumentId,
    pub side: Side,
    pub qty: u32,
    pub price: f64,
    pub timestamp_ns: u64,              // exchange clock if available
    pub commission: Option<f64>,
}

pub struct OrderStatusUpdate {
    pub order_id: OrderId,
    pub status: OrderStatus,            // Pending | Submitted | Filled | Cancelled | Rejected
    pub filled_qty: u32,
    pub remaining_qty: u32,
    pub avg_fill_price: f64,
    pub last_fill_price: Option<f64>,
    pub timestamp_ns: u64,
    pub reject_reason: Option<String>,
}

pub struct Tick {
    pub instrument_id: InstrumentId,
    pub kind: TickKind,                 // Bid | Ask | Last | BidSize | AskSize | Volume
    pub price: Option<f64>,
    pub size: Option<u64>,
    pub timestamp_ns: u64,
}

pub struct Position {
    pub instrument: Contract,
    pub quantity: i32,                  // signed: positive=long, negative=short
    pub avg_cost: f64,                  // basis price
    pub realized_pnl: f64,
    pub unrealized_pnl: f64,
}

pub struct AccountSnapshot {
    pub net_liquidation: f64,
    pub maintenance_margin: f64,
    pub initial_margin: f64,
    pub buying_power: f64,
    pub realized_pnl_today: f64,
    pub timestamp_ns: u64,
}
```

### 5.3 Implementations (now and future)

```rust
// rust/corsair_broker_ibkr/src/lib.rs
pub struct IbkrAdapter {
    // Phase 2-5: bridged through Python ib_insync via PyO3 (cheap path
    //            to validate the trait's surface is right).
    // Phase 6+:  native IBKR API V100+ wire client. Then PyO3 dies.
}

impl Broker for IbkrAdapter { /* ... */ }

// rust/corsair_broker_ilink/src/lib.rs   (FUTURE)
pub struct IlinkAdapter {
    fix_session: FixSession,            // QuickFIX-RS or hand-rolled FIX 4.2
    mdp3_reader: Mdp3Reader,            // CME market data (separate session)
    drop_copy: DropCopyConsumer,        // fills via FCM drop-copy
    span_calc: corsair_pricing::SpanCalc,  // pre-trade SPAN gating
}

impl Broker for IlinkAdapter { /* ... */ }
```

### 5.4 Trait surface review (this is the bar before Phase 1 starts)

Before any Rust crate consumes this trait, the surface must:
- Express every operation our current Python code performs against ib_insync
  (so swap-to-IBKR-Rust is mechanical)
- Express every operation iLink/MDP3/drop-copy will need (so swap-to-FCM
  is bounded to the adapter)
- Hide every IBKR quirk (FA-rewriting orderKey, multiple Trade objects per
  orderId, magic strings) from consumers

**Action**: cross-check trait against `quote_engine.py`, `fill_handler.py`,
`hedge_manager.py`, `market_data.py` line-by-line before sign-off.

## 6. Crate / process structure

### 6.1 Cargo workspace

```
rust/
├── Cargo.toml                   workspace root
├── corsair_pricing/             ✅  Black-76, IV, SABR/SVI, calibrate, greeks, SPAN
├── corsair_trader/              ✅  trader binary (decision loop, JSONL, IPC client)
├── corsair_broker_api/          NEW Broker trait + value types
├── corsair_broker_ibkr/         NEW IBKR adapter (Phase 2-5: PyO3 bridge → native)
├── corsair_broker_ilink/        FUTURE FIX 4.2 + MDP3 + drop copy
├── corsair_ipc/                 NEW shared IPC (extract from corsair_trader)
├── corsair_position/            NEW PortfolioState + per-product greek aggregation
├── corsair_oms/                 NEW order lifecycle, our_orders index, GTD tracking
├── corsair_market_data/         NEW tick → state, ATM tracking, refit triggers
├── corsair_risk/                NEW kill switches, induced sentinels, daily P&L halt
├── corsair_constraint/          NEW margin/delta/theta/vega gating
├── corsair_hedge/               NEW HedgeManager + HedgeFanout, lockout-skip
├── corsair_snapshot/            NEW dashboard JSON publisher (4 Hz)
└── corsair_broker/              NEW *the* broker daemon binary that wires above
```

### 6.2 Process layout

```
docker-compose services (post-Phase 5):

  ib-gateway       — third-party IBKR Gateway image (unchanged)
  corsair-broker   — Rust binary; replaces today's `corsair` Python service
  corsair-trader   — Rust binary; unchanged from today
  dashboard        — Streamlit, reads snapshot.json + JSONL streams
```

Phase 7 (FCM/iLink), the broker daemon's adapter switches; service layout
unchanged. Add an Aurora-colocated network path; the rest of the topology
stays put.

## 7. Configuration model

### 7.1 Runtime config (Rust reads, humans + research write)

```yaml
# config/runtime.yaml
broker:
  kind: ibkr                    # or "ilink" (future)
  ibkr:
    gateway:
      host: 127.0.0.1
      port: 4002
    client_id: 0
    account: DUP553657
  # When ilink: separate iLink/MDP3/drop-copy sub-blocks

risk:
  daily_pnl_halt_pct: 0.05      # primary v1.4 defense
  margin_kill_pct: 0.70
  delta_kill: 5.0
  theta_kill: -500
  vega_kill: 0                  # disabled per Alabaster

constraints:
  capital: 500000
  margin_ceiling_pct: 0.50
  delta_ceiling: 3.0
  theta_floor: -500
  effective_delta_gating: true  # use options + hedge_qty

products:
  - name: HG
    multiplier: 25000
    quote_range_low: -5
    quote_range_high: 5
    enabled: true

quoting:
  tick_size: 0.0005
  min_edge_ticks: 2
  max_strike_offset_usd: 0.30
  gtd_lifetime_s: 30

hedging:
  enabled: true
  mode: execute
  tolerance_deltas: 0.5
  rebalance_cadence_sec: 30
  ioc_tick_offset: 2
  hedge_lockout_days: 30        # split from near_expiry per CLAUDE.md §10
```

### 7.2 Parameter files (research outputs)

```toml
# config/params/sabr_seed_hg.toml
# Auto-generated by research/sabr_seed_search.py on YYYY-MM-DD
[[guesses]]
alpha = 0.45
rho   = -0.30
nu    = 1.20

[[guesses]]
alpha = 0.50
rho   = -0.50
nu    = 1.50
# ...
```

The Rust calibrator reads this at startup; research can update without
touching production code. Versioned via git so every fit run is auditable.

## 8. Migration phases

Each phase is independently shippable. Each ends with the system live,
running, and rollback-safe.

### Phase 0 — Done (today's state)

- ✅ corsair_pricing: math hot path
- ✅ corsair_trader: full Rust trader binary
- Python broker + ib_insync owns everything else

### Phase 1 — Broker trait surface lock (1 week)

**Output**: `corsair_broker_api` crate. Trait + value types. Compiles, tested
against a `MockBroker` that records calls.

**Validation**: cross-check trait against current Python code line-by-line.
Document any IBKR-only quirks the trait deliberately hides
(orderKey rewriting, multiple Trade objects, FA account field, etc.).

**Rollback**: nothing live changes; this is pure spec work.

### Phase 2 — IBKR adapter (PyO3 bridge) (2 weeks)

**Output**: `corsair_broker_ibkr` implementing `Broker` trait. Internally
calls into Python ib_insync via PyO3 (Rust → Python is fine; the bridge is
thin and lives at the edge).

**Why bridge first**: validates the trait surface is correct against the
real IBKR semantics without forcing a native rewrite up front.

**Rollback**: Python broker keeps running; bridge is built but not yet
consumed.

### Phase 3 — Stateful Rust modules (3-4 weeks)

Build in this order (each ~3-5 days):

1. `corsair_position` — PortfolioState struct, per-product greek aggregation,
   refresh loop. Drives off broker fill/status streams.
2. `corsair_risk` — kill switches, induced sentinels, daily P&L halt.
   Subscribes to corsair_position + broker connection events.
3. `corsair_constraint` — margin/delta/theta/vega gates. Uses Rust SPAN
   already in corsair_pricing. Returns gate decisions to OMS.
4. `corsair_hedge` — HedgeManager + HedgeFanout. Driver-style; calls
   broker.place_order for hedges.
5. `corsair_oms` — order lifecycle, our_orders index, GTD refresh, the
   send-or-update / cancel-before-replace logic. Replaces quote_engine.py.
6. `corsair_market_data` — tick stream → option state, ATM tracking,
   strike subscription window, refit triggers.
7. `corsair_snapshot` — 4 Hz JSON publisher for dashboard.

**Validation pattern**: each module has inline tests + a parity test in
Python (mirror today's `test_*_parity.py` pattern). The Python broker can
optionally consume Rust modules via PyO3 during this phase for shadow
validation.

**Rollback**: one Rust crate at a time can fall back to Python via env var,
exactly as today's `CORSAIR_GREEKS=python` etc.

### Phase 4 — corsair_broker daemon (1-2 weeks)

**Output**: `corsair_broker` binary. Wires Phases 2-3 into a single Rust
process with the same external behavior as today's Python broker.

**Validation**: shadow mode. Rust broker reads same tick stream, publishes
its decisions to a separate JSONL stream (`broker_decisions_rust-*.jsonl`),
does NOT place orders. Compare its decisions to live Python broker's for
1-2 sessions. Drift is logged; root-causes are fixed.

**Rollback**: shadow mode runs in parallel. Switching live = compose flag.

### Phase 5 — Cut-over (1 week)

**Output**: Rust broker is sole production runtime. Python broker stops.
src/ collapses to research/ + scripts/. Streamlit dashboard is the only
Python service running.

**Validation**: same pattern as today's mm_service_split cut-over. Preflight
checklist, live monitor, rollback path.

**Post-cut-over cleanup**:
- Delete src/main.py, src/quote_engine.py, src/fill_handler.py, etc.
  (their logic now lives in Rust crates)
- Move tests/ that test Python orchestration to research/legacy_tests/
- Update CLAUDE.md §15 to reflect new topology

### Phase 6 — Native Rust IBKR client (3-4 weeks)

**Output**: `corsair_broker_ibkr` no longer uses PyO3. Native IBKR API V100+
wire client. ib_insync is uninstalled.

**Why deferred**: only worth doing once the surface around it is stable. If
we did this in Phase 2 we'd be debugging both the wire client AND the trait
boundary at once.

**Validation**: parity tests against ib_insync replay tapes. Real-account
shadow run for 2-3 sessions before cut-over.

**Rollback**: keep PyO3 bridge available behind a feature flag for one
release cycle.

### Phase 7 — iLink adapter (FUTURE, 4-6 weeks, conditional on FCM signup)

**Output**: `corsair_broker_ilink` as a second Broker impl. Wires:
- iLink FIX 4.2 session (QuickFIX-RS or hand-rolled)
- MDP3 market data reader
- Drop-copy session for fills
- Pre-trade SPAN gating using corsair_pricing
- Sequence number / replay handling

**Triggering condition**: FCM relationship live + Aurora cross-connect
provisioned + sufficient volume to justify the operational burden.

**Cut-over**: change `broker.kind: ibkr → ilink` in runtime.yaml, restart.

## 9. Testing strategy

### 9.1 Three layers

1. **Inline Rust tests** (`#[cfg(test)] mod tests`): each module's correctness
   in isolation. Already established in corsair_pricing/corsair_trader.
2. **Parity tests** (`tests/test_*_parity.py`): Python and Rust paths produce
   numerically equivalent output across random inputs. Already established
   for pricing/calibrate/greeks/SPAN.
3. **Shadow integration tests**: full Rust runtime against real IBKR paper
   account, decisions logged but not placed. Compare to live Python broker.

### 9.2 Replay infrastructure (NEW)

For Phase 2+ we need a tick-replay harness: record a session's market data
+ order events as JSONL, replay through the Rust runtime, compare decisions.

**Components**:
- `record_tape.py` — Python script that subscribes to a paper IBKR session
  and writes a complete tape (ticks + fills + status) to JSONL.
- `replay_tape` — Rust binary (in corsair_broker, dev-only feature flag)
  that ingests a tape and runs the Rust runtime against it without a real
  broker connection.
- `compare_decisions.py` — diffs Python broker decisions vs Rust replay
  decisions for the same tape. Flags drift.

This is a Phase 1.5 deliverable — built before Phase 3 starts so each
new Rust module has immediate regression coverage.

### 9.3 Property tests

For mathy code (already covered by corsair_pricing tests) extend property
testing into stateful modules:
- "Inserting a fill never reduces position count by more than 1"
- "Greeks refresh produces same output for unchanged positions"
- "Daily P&L halt fires within 1 cycle of threshold breach"

Use `proptest` crate (Rust) for these.

## 10. Risk and rollback

### 10.1 Per-phase rollback

| Phase | Rollback mechanism | Recovery time |
|---|---|---|
| 1 | Discard the trait crate; nothing live changed | n/a |
| 2 | Bridge unused unless explicitly enabled | n/a |
| 3 | Each module env-flag fallback to Python | <1 min/restart |
| 4 | Shadow-mode binary — never had real auth | n/a |
| 5 | Compose flag flips back to Python broker | <2 min restart |
| 6 | Feature flag re-enables PyO3 bridge | <2 min restart |
| 7 | Config flag flips back to ibkr adapter | <2 min restart |

### 10.2 Stop conditions

Halt the migration if any of these happen:
- A live regression that took >2 days to root-cause
- Two consecutive phases require >50% schedule overrun
- Operator finds the spec materially wrong about an external system
  (IBKR FA quirks, FIX session details we missed)

In those cases: re-scope the spec, don't push through.

### 10.3 Live-system invariants (NEVER violated)

Throughout migration, these MUST hold every session:
- Daily P&L halt remains armed and tested (Gate 0 induced sentinels)
- Position book reconciles to IBKR within 30s of every restart
- Hedge subsystem (CLAUDE.md §10) stays in execute mode with reconciliation
- v1.4 spec deviations stay at the documented set in CLAUDE.md
- Kill-switch JSONL stream is continuous (no migration-induced gaps)

## 11. Operational concerns

### 11.1 Logging continuity

JSONL stream filenames and schemas are stable across the migration. A
researcher reading `fills-2026-09-01.jsonl` can't tell whether it was
written by today's Python broker or by the future Rust broker — same
schema, same name pattern, same record types.

**Schemas live in** `docs/streams/` as JSON Schema documents, validated
in CI against both Python and Rust writers.

### 11.2 Health and observability

The Rust broker emits the same telemetry topology as today's Python broker:
- Per-cycle TTT histogram (already done in trader)
- Per-cycle quote count, eval count, skip-reason breakdown
- Connection events (reconnects, gateway state)
- Risk-state dump every 1 Hz (already wired for trader)

Plus new (Phase 5+):
- `/state` Unix socket endpoint exposing live state for ops tools

### 11.3 Deploy + restart procedure

Same as today's pattern:
- `docker compose up -d --build corsair-broker`
- Rust broker starts, reconciles position book from IBKR/FCM, replays open
  orders, resumes quoting
- Watchdog clears any disconnect-source kills automatically
- Risk-source kills remain sticky (operator review required)

## 12. Success criteria

The migration is "done" when:

1. ✅ Rust broker runs production for 5 consecutive paper sessions with
   zero migration-related kills
2. ✅ TTT p50 ≤ 200 µs and p99 ≤ 1.5 ms (matches or beats current Rust trader)
3. ✅ All 8 v1.4 JSONL streams continuous and schema-stable
4. ✅ Induced kill switch tests pass at boot every session (Gate 0)
5. ✅ Position book reconciles to IBKR within 30s of every restart
6. ✅ ib_insync fully removed from production wheel
7. ✅ Switching IBKR → iLink adapter (when ready) is a single config change

Phase 7 (iLink) success when: same criteria, against an FCM/iLink session.

## 13. Out of scope for v3

- Strategy changes (quoting cadence, edge sizing, regime detection)
- Multi-product simultaneous trading (HG-only stays the operational scope)
- Cross-venue (CME-only stays the venue scope)
- Real-time hot config reload (restart is the unit of change)
- Web-based admin UI (Streamlit dashboard is sufficient)

These are valuable but separate work. v3 is structural; the strategy and
operational scope it runs are unchanged.

## 14. Open questions

These need resolution before Phase 1 sign-off:

1. **Async runtime**: tokio multi-thread (today's trader) or tokio single-thread
   for the broker? Single-thread reduces lock contention but the broker
   also runs blocking-ish work (snapshot serialization). Lean toward
   multi-thread with task pinning for the hot path.
2. **PyO3 bridge in Phase 2 — fully blocking calls or async-bridged?** ib_insync
   is asyncio. Easiest path: call into Python from a dedicated thread that
   owns the asyncio loop, bridge results back via tokio channels. Adds
   one layer of latency but isolates the Python event loop.
3. **Drop-copy redundancy** (Phase 7): if the FCM's drop copy lags or fails,
   what's the fallback? Re-query positions periodically from the iLink
   session? Or treat drop-copy as authoritative and pause-on-stale?
4. **Native IBKR client** (Phase 6): roll our own TCP/binary protocol
   client, or fork an existing crate (`ibapi`, `ibtwsapi`)? Existing
   crates are minimal but unmaintained.
5. **Test data volume**: how many tape replay sessions do we need to cover
   regime variety (normal RTH, gappy open, post-close, vol spikes)?

## 15. Approval

This spec proposes 14-18 weeks of work, replacing the production runtime
core while preserving every v1.4 invariant.

Before Phase 1 starts, the operator should:

- [ ] Review §3 architecture and §5 Broker trait — these are the costliest
      to change later
- [ ] Confirm §4 boundary contract (file-based + JSONL only, no RPCs)
- [ ] Approve §8 phasing and rollback model
- [ ] Identify open questions in §14 to resolve up front

---

## Appendix A — Mapping current files to v3 crates

For migration planning. Each Python file's logic lands in exactly one
Rust crate (or in research/, if it's not runtime).

| Today's file (Python) | LOC | v3 destination | Notes |
|---|---:|---|---|
| `src/main.py` | 1402 | `corsair_broker` (binary) | bootstrap+wiring |
| `src/connection.py` | ~150 | `corsair_broker_ibkr` | lean connect bypass |
| `src/market_data.py` | 1420 | `corsair_market_data` | + sub-window mgmt |
| `src/quote_engine.py` | 2779 | `corsair_oms` | order lifecycle |
| `src/quote_engine_helpers.py` | 219 | `corsair_oms` | utilities |
| `src/position_manager.py` | ~700 | `corsair_position` | + greek refresh |
| `src/fill_handler.py` | 463 | `corsair_position` | fold into position |
| `src/risk_monitor.py` | 585 | `corsair_risk` | kill switches |
| `src/operational_kills.py` | 359 | `corsair_risk` | latency/RMSE/abnormal |
| `src/hedge_manager.py` | 904 | `corsair_hedge` | full port |
| `src/constraint_checker.py` | 634 | `corsair_constraint` | gates |
| `src/snapshot.py` | 486 | `corsair_snapshot` | 4 Hz publisher |
| `src/sabr.py` | 1307 | research/ + `corsair_pricing` | calib stays Rust |
| `src/pricing.py` | ~150 | research/ | thin Black-76 wrapper |
| `src/greeks.py` | ~135 | research/ | thin wrapper, Rust handles |
| `src/synthetic_span.py` | ~225 | research/ | thin wrapper, Rust handles |
| `src/watchdog.py` | 590 | `corsair_broker` | in-binary subsystem |
| `src/loop_block_detector.py` | ~100 | `corsair_broker` | in-binary subsystem |
| `src/daily_state.py` | ~200 | `corsair_position` | persistence |
| `src/daily_summary.py` | ~150 | research/post_trade/ | offline reporting |
| `src/discord_notify.py` | ~80 | `corsair_broker` | in-binary subsystem |
| `src/ipc/shm.py` | 465 | `corsair_ipc` (extracted from trader) | shared crate |
| `src/broker_ipc.py` | ~600 | `corsair_broker` | trader-IPC server |
| `src/trader/*` | ~2000 | already Rust | done |
| `src/ib_insync_patch.py` | 120 | DELETED at Phase 6 | quirk patches go away |
| `src/utils.py` | ~400 | research/ + small bits to crates | utility split |
| `src/config.py` | ~200 | `corsair_broker` | YAML loader |
| `src/logging_utils.py` | 735 | `corsair_broker` (or split) | JSONL writers |
| `src/startup_checks.py` | ~150 | `corsair_broker` | preflight |
| `src/backmonth_surface.py` | ~250 | research/ | TSI fallback |
| `src/weekend.py` | ~100 | `corsair_broker` | session calendar |
| **Total runtime Python** | **~16,500** | **→ ~14 Rust crates** | |

Tests and ops scripts move to `research/tests/` and `ops/` respectively;
they stay Python.

---

## Appendix B — Comparison to alternative architectures

For posterity. Considered and rejected:

**B.1 Full Rust monolith (no Python)**
Move research / backtesting / dashboards to Rust too. Rejected: research
iteration speed in Rust is 5-10× slower than Python; the offline tools
don't pay the Rust tax in latency or correctness.

**B.2 Python-Rust microservices over gRPC**
Rust services and Python services talk over gRPC. Rejected: introduces
RPC latency on the hot path; deployment complexity multiplies; doesn't
solve the "ib_insync is in production" problem.

**B.3 Embed Rust runtime inside the Python process via PyO3 forever**
Phase 2's pattern, but never graduate. Rejected: PyO3 keeps the GIL in
the loop. Native Rust trader already proved the cost; rolling that back
into a Python process loses ~5× of the speedup we just shipped.

**B.4 Replace ib_insync with a different Python IBKR client**
Faster Python clients exist (ibapi raw, async wrappers). Rejected:
addresses the symptom (ib_insync overhead), not the cause (Python in
production runtime). Each subsequent migration would still hit this.
