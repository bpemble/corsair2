# HG Front-Only Market-Making: Full Specification & Handoff

Date: 2026-04-18
Author: Crowsnest research
Status: **Handoff document. Corsair-side implementation spec.**
Sources: Phase 4 U1/U2, HG TAM analysis, per-strike and theta/vega
         diagnostics, deployment prep checklist. See §22 for
         full artifact index.

---

## 0. How to use this document

This spec is exhaustive on purpose. It captures every economic
detail from the research so corsair team can implement without
further back-and-forth.

Three ownership lanes:
- **Crowsnest (research) owns**: the plan, success criteria,
  reconciliation infrastructure, post-deployment monitoring on
  the research side.
- **Corsair (execution) owns**: IBKR integration, live SABR
  fitting, order management, kill-switch implementation, quote
  flow.
- **Interface**: paper-trading fill log schema (§17),
  reconciliation ingestion path (§18).

Sections 1-7 are the product and strategy specification. Sections
8-10 are the risk framework. Sections 11-15 are deployment ramp
and operations. Sections 16-20 are reconciliation and monitoring.
Sections 21-22 are projections and artifacts.

---

## 1. Decision context

**What is being built**: a live market-making operation on CME HG
copper options, front-month only, quoting both calls and puts
across a symmetric strike range. Goal: capture theta on a short-
vol book with path-dependent delta risk management.

**What is explicitly NOT being built**:
- ETH market-making (see retraction memo
  `docs/findings/realism_upgrades/eth_v4_spec_retraction_2026-04-18.md`
  — target was arithmetically infeasible given observable ETH
  options TAM of 64 contracts/day total across front 3 months)
- HG back-month quoting (front+1 Sharpe −3.58 in Phase 4,
  $2.16M in-sample max drawdown; not rescuable within current
  framework)
- Wings-only restriction from v1.1 spec (superseded; was argued
  on back-month-adverse framing that no longer applies)
- Hedging enabled at Stage 1-2 (deferred to Stage 3+ decision)
- Any Phase 6 strategy variant (tick-jumping, Avellaneda-Stoikov)

**Why now**: Phase 4 research on clean post-fix data shows HG
front-only in-sample Sharpe +7.10, max DD $14,595 over 4 months,
~$2.95M/yr annualized. TAM analysis confirms 5.5% per-contract
penetration is inside MM-literature comfort zone with scaling
headroom. Per-strike concentration (20% median) is a live-data
watch item but does not block deployment.

---

## 2. Product specification — CME HG copper options

### 2.1 Underlying contract

- **Exchange**: CME Group (CME Commodity Exchange / COMEX division)
- **Product**: Copper futures (symbol HG)
- **Contract size**: 25,000 pounds copper
- **Tick size**: $0.0005/lb = **$12.50 per tick per contract**
- **Contract months**: All 12 months; primary (delivery) months
  are **H, K, N, U, Z** (March, May, July, September, December)
- **Settlement**: Physical delivery
- **Trading hours (UTC)**: Sun 22:00 → Fri 21:00 with daily break
  21:00-22:00 UTC (Chicago 17:00-18:00)

### 2.2 Options contract (HXE series)

- **Symbol prefix**: `HXE` followed by month letter + year digit
  (e.g., HXEH6 = March 2026)
- **Contract size**: 1 HG futures contract (25,000 lbs)
- **Tick size**: $0.0005 per lb = **$12.50 per tick**
- **Strike increments**: **$0.01 (penny)** throughout the active
  strike range. Empirical verification: 101 unique strikes exist
  in the $5.00-$6.00 range on the Databento panel.
  **Volume strongly concentrates on nickel strikes** (multiples
  of $0.05) — see §3.3a.
- **Exercise style**: American
- **Expiry**: 4 business days before the underlying HG futures
  last trading day (approximately month-end)
- **Quote format**: `HXE[M][Y] [CP][strike]` e.g.
  `HXEH6 C485` = March 2026 call $4.85/lb
  - Note: strike in quotes appears in cents × 100 convention
    (485 = $4.85/lb). Corsair's existing HG integration already
    handles this via `strike_scale=100.0` in the ProductSpec.

### 2.3 Front-month identification

**Canonical definition** (for daily operations):
- The primary delivery-month contract with highest event count
  that calendar day is "front" for that day.
- Transition: every ~35 days empirically (primary-month cycle).
- Example transitions observed in 2025-10 → 2026-03 panel:
  - Through 2025-11-22: HXEZ5 (Dec 2025) is front
  - 2025-11-23 → 2026-02-22: HXEH6 (March 2026) is front
  - 2026-02-23 → ongoing: HXEK6 (May 2026) is front

**Front-only filter** applied at quote time:
- DTE ≤ 35 days to the contract's expiry
- Do NOT quote any contract with DTE > 35 regardless of its
  activity rank

### 2.4 Market context (6-month 2025-10 → 2026-03 measured panel)

Daily HG front-month options volume distribution:
- **Median: 991 contracts/day**
- p25: 580 / p75: 1,361 / p95: 2,286
- Typical active strikes: 81/day at TAM level

Top-of-book structure (observed):
- Median 1 quoter per side
- p90: 4 quoters on bid, 3 on ask
- Max: 21 bid / 9 ask

---

## 3. Strategy specification — quote policy

### 3.1 Core policy

**Penny-jump**: quote 1 tick inside the incumbent BBO on each
side, subject to the min_edge constraint and BBO width filter.

### 3.2 Sides quoted

- Both calls AND puts, every active front-month strike in scope
- Shared capital / margin / risk budget across C+P (no per-side
  allocation; the book is one combined position)

### 3.3 Strike scope

**Target**: ATM ± 5 NICKEL strikes on each of calls and puts.

- "ATM" = nickel strike nearest to current forward (futures mid)
- "±5 strikes" = counting NICKEL strikes only (multiples of
  $0.05). Non-nickel strikes ($0.01 penny grid) are ignored.
- Total strikes quoted simultaneously: **~10-11 nickel strikes
  per side** (calls + puts)
- Recompute ATM every quote cycle (30s default); re-center strike
  window on material underlying moves

**Not quoted**:
- DTE > 35
- Strikes outside ATM ± 5 nickel window
- **Any non-nickel strike** (penny strikes like $5.13, $5.27 —
  ignored)
- Strikes where SABR fit quality (RMSE) exceeds 0.05
- Strikes outside the "calibrated range" (SABR fit domain
  per `volatility.py`'s `is_strike_calibrated`)

### 3.3a Why nickel-strike filter

Empirical HG strike-grid analysis on Databento 6-month panel
(see §22 for artifact reference):

| Strike type | Unique | Total contracts | Share of volume |
|---|---|---|---|
| Nickel ($0.05 multiples) | 134 | 237,765 | **82.3%** |
| Non-nickel (penny grid) | 188 | 51,261 | 17.7% |

Top 30 strikes by volume are 100% nickels. Non-nickel strikes are
structurally thinner and, at corsair's scale, would push
per-strike TAM penetration into the MM-literature >15%
adversity zone faster. Filter to nickels:
- Retains 82% of traded volume
- Improves per-strike TAM (corsair is one of many on each nickel
  strike; on penny strikes it could be the only MM)
- Simplifies operations (fewer strikes to quote, refresh, cancel)

### 3.3b Phase 4 backtest did NOT apply this filter

The projection numbers elsewhere in this spec (Sharpe +7.10,
55 fills/day on front) come from a backtest that quoted the full
strike grid, including non-nickel strikes. Phase 4 fills:
- 72% on nickel strikes (consistent with market-level volume
  distribution)
- 28% on non-nickel strikes (captured some but produced thinner
  per-strike activity)

Adopting the nickel-only filter at production likely:
- Reduces fills/day by ~15-20% from the simulated 55 → **expect
  45-47 fills/day live on nickel-only policy**
- Improves per-strike TAM dynamics (reduced AS risk)
- Net P&L impact: probably −10% vs unrestricted (loses some
  volume, but the volume lost is the thinnest, highest-AS-risk
  volume)

If Stage 1-2 live data shows the nickel filter unnecessarily
conservative, expand to include the highest-volume non-nickel
strikes (dimes without nickels: $5.11, $5.13, etc.) as a Stage 3
decision. Do NOT start unrestricted.

### 3.4 Quote parameters

| Parameter | Value | Source |
|---|---|---|
| Quote size | **1 lot** | v1.1 spec; supports Stage 1-2 capacity |
| min_edge_points | **0.001** | Phase 4 measured profitable at this edge on front |
| BBO width filter | **Skip if market bid-ask > $10/contract wide** | v4 spec; stops quoting through wide markets |
| Refresh interval | **≤ 30 seconds** | Matches SABR calibration cadence |
| Inventory skew | **None at Stage 1-2** | Keep parameter count minimal; add later if needed |
| base_spread_ticks | **TBD — corsair to confirm from v1.1** | Per-tick half-spread target |
| max_spread_ticks | **TBD — corsair to confirm from v1.1** | Upper-bound half-spread |

### 3.5 Fill share at top

When corsair's quote is at market BBO:
- Share = 1 / quoter_count for same-side competing quoters
- When `quoter_count = 1` (corsair alone), share = 100%
- Empirical observation from Phase 4: median quoter_count = 1 on
  both sides → corsair typically gets full share

### 3.6 Measured fill-rate economics (Phase 4 in-sample, 4mo)

| Metric | Value |
|---|---|
| Fills/day on active days | 55 (median 55.5) |
| Active front-months in window | 4 distinct contracts |
| Total fills | 989 |
| Fills as % of corsair-eligible trade events | 29.2% |

Non-fill events breakdown (why corsair didn't fill on a trade):
- Market too wide (`wide_market` skip): most common
- One-sided market (`no_two_sided_market`): secondary
- Below min_edge: infrequent

---

## 4. Theo and pricing — SABR

### 4.1 Theo source

**Live**: corsair's production SABR fitter (`~/corsair/src/volatility.py`)
calibrated to IBKR market implied vols.

- Model: SABR with beta=1.0 fixed, (alpha, rho, nu) calibrated
- Fallback: quadratic log-moneyness when SABR RMSE > threshold
- Fallback-fallback: flat vol when < 3 strikes available

### 4.2 Calibration cadence

- Update interval: 30 seconds (per `settings.yaml` default)
- Source: IBKR real-time option implied vols (market quotes)
- Staleness weighting: quotes < 60s full weight, 60-300s linearly
  decayed to 0.2, >300s = 0.2

### 4.3 RV shade (DO NOT enable at Stage 1-2)

Current `settings-live.yaml` has `rv_shade_strength: 0.0`. Keep
disabled. Crowsnest research has not validated whether RV shading
helps or hurts on HG specifically; live-without-shade is the
baseline for reconciliation.

If considered later: shade enables a realized-vol-vs-implied-vol
correction to theo. Evaluate post-Stage-2 with actual live data.

### 4.4 Pricing model — Black-76

Options priced via Black-76 (no cost-of-carry on futures options).
Implementation already exists in `~/corsair/src/pricing.py`.
Crowsnest mirror in `src/crowsnest/backtest/svi_theo.py` for
reconciliation.

---

## 5. Hedging

### 5.1 Stage 1-2: OFF

**Hedging is OFF during Stage 1 smoke test and Stage 2 validation.**

Rationale:
- Front-month options have low vega per contract (short DTE)
- Daily delta rebalancing (if needed) handles directional drift
- Reduces operational complexity during initial validation
- v4 ETH spec enabled hedging at Stage 4; HG Stage 2 is an
  earlier gate with tighter risk limits

### 5.2 Stage 3+: decision TBD

When Stage 3 is considered (post-Stage-2 pass), hedging decision
is a separate review. Candidates:
- v4-style: `|total_delta| > 5.0` trigger, ETH futures hedge
- HG-equivalent: `|total_delta| > 3-5` trigger, HG futures hedge
- Alternative: per-position auto-close on delta breach
  (avoid accumulated-delta problem entirely)

### 5.3 Delta measurement (even when hedging off)

Even without hedging, delta must be tracked for kill-switch
purposes:
- `options_delta`: sum of Black-76 deltas × position × multiplier
  for all open options positions
- `futures_delta`: zero at Stage 1-2 (no hedge)
- `total_delta = options_delta + futures_delta`
- Delta kill at **±5.0** (Tier 1) / delta ceiling at **±3.0**
  (Tier 2) applies to `total_delta`.

---

## 6. Constraint framework — Tier 1 (hard) / Tier 2 (soft)

Adopted from v4 spec's tiered constraint architecture. Tier 1
kills are never breachable. Tier 2 constraints can yield during
margin-escape mode.

### 6.1 Tier 1 — hard kills (never yield)

Stage 1 values (adjust proportionally at later stages, see §12):

| Constraint | Stage 1 value | Config key | Behavior |
|---|---|---|---|
| SPAN margin kill | **$140K** (70% × $200K capital) | `kill_switch.margin_kill_pct` | Absolute halt; flatten book |
| Total delta kill | **±5.0** | `kill_switch.delta_kill` | Absolute halt |
| Options-delta kill (hedge-fail detector) | N/A at Stage 1 (no hedge) | `kill_switch.delta_kill_options` | Stage 3+ |
| Theta kill | **−$500** | `kill_switch.theta_kill` | Absolute halt |
| Daily P&L halt | **−$2,500** | `kill_switch.daily_pnl_halt` | Cancel all, stop for day |
| Long premium capital | **$200K** (full capital) | `capital` | Cash outlay cap |
| |Net vega| halt | **$1,000** | `kill_switch.vega_halt` | Cancel all |
| IBKR gateway disconnect | N/A | connection monitor | Cancel all resting |

### 6.2 Tier 2 — soft constraints (yield in margin-escape)

| Constraint | Stage 1 value | Config key |
|---|---|---|
| Net delta ceiling | **±3.0** | `delta_ceiling` |
| Net theta floor | **−$200** | `theta_floor` |

Improving-fill exception applies at all times: a fill that moves
the violated constraint in the right direction is allowed even if
it doesn't bring it under the ceiling.

### 6.3 Margin-escape mode

`margin_escape_enabled: true` at ALL stages.

Behavior: when current margin > Tier 1 ceiling AND a fill strictly
reduces margin, Tier 2 soft constraints are suspended for that
fill. Tier 1 kills remain binding. Prevents the 2026-04-09 wedge
scenario documented in v4.

### 6.4 Improving-fill exception logic

For any Tier-1 or Tier-2 constraint currently in breach:
- **Margin**: `post ≤ current` while `current > ceiling` → allowed
- **Delta**: `|post| < |current|` while `|current| > ceiling` → allowed
- **Theta**: `post ≥ current` while `current < floor` → allowed

Applied independently per constraint. Matches v4 spec semantics.

---

## 7. Operational kill switches (outside tier framework)

These are infrastructure-level safeguards, not strategy
constraints:

| Switch | Trigger | Action |
|---|---|---|
| IBKR gateway disconnect | Any loss of connection | Cancel all resting orders |
| SABR calibration failure | RMSE > 0.05 for 5+ min on front-month | Halt quoting on affected strikes |
| Quote latency breach | Median quote-update latency > 2s for 60s | Halt all quoting, page operator |
| Position reconciliation failure | IBKR-reported position ≠ corsair's internal state | Halt, reconcile, page operator |
| Realized vol > 2σ from in-sample mean | 5-day rolling rvol exceeds 0.326 + 2×0.136 = 0.598 | Page (do not auto-halt; research-side signal) |

---

## 8. Capital structure

### 8.1 Allocated capital

- **Total operating capital committed: $500K** (Stage 2 target state)
- Broken down:
  - Stage 2 margin cap: $250K
  - Working buffer (intra-day margin swings): $150K
  - Kill-switch reserve (locked, never deployed): $100K

### 8.2 Margin utilization expectations

From v4 spec backtest measurements on similar capital scales:

| Stage | Margin cap | Backtest mean (low vol) | Backtest mean (high vol) |
|---|---|---|---|
| 1 | $100K | 145% | 151% |
| 2 | $250K | 121% | 136% |

**Stages 1-2 are constraint-saturated at small capital** —
margin-escape mode will fire frequently. This is a feature, not
a bug, but it means capture rate will be lower than Stage 3+.

### 8.3 Expected IBKR margin requirements

HG options SPAN margin is regime-dependent. Order of magnitude
for a 200-contract short straddle at ATM:
- Initial margin: ~$100K-$150K
- Maintenance: ~$80K-$120K
- Moves within $50K / day in normal conditions

Corsair side: verify via IBKR's SPAN calculator on a
representative HG position before Stage 1 starts. Crowsnest does
not have access to live SPAN computation.

### 8.4 ⚠ CRITICAL: Margin-awareness of projected numbers

The Phase 4 backtest which produced Sharpe 7.10, per-fill $993
net, and the scenario projections in §13 **does NOT model SPAN
margin binding**. Specifically, the backtest has:

- Per-(contract, cp, strike) position cap of 5 lots
  (`max_pos_per_key = 5`)
- Daily P&L tracking (option MTM, hedge MTM, rebalance cost)
- Delta / theta / vega tracking

And **does NOT model**:
- SPAN margin calculation
- Margin-escape mode triggering
- Position closing or quote-skipping from margin ceiling approach
- Kill switch interruptions
- Constraint-saturation effects at small capital

**Live implications at $250K margin (Stage 2)**:
- v1.1 spec measured backtest margin utilization 121-136% mean of
  cap — constraint-saturated most of the time
- When margin binds, corsair can't take new same-side fills → fill
  count drops
- v4 spec measured: **Stage 1 capture rate drops to ~10%** from
  ~30% unconstrained specifically because of constraint-saturation
- Kill switch trips cost quote-time; each trip = several minutes
  of missed fills

**Post-hoc SPAN proxy on Phase 4 fills (2026-04-18 v1.2 measurement)**:
Simple per-contract margin proxy (12% × notional for short legs,
full premium for longs; no SPAN netting benefit):

| Margin cap | Fills that would be blocked (upper bound) |
|---|---|
| $100K (Stage 1) | 99.4% |
| **$150K (proposed Stage 1.5)** | **99.1%** |
| $200K | 98.8% |
| **$250K (Stage 2)** | **98.5%** |
| $350K | 98.0% |
| $500K | 97.3% |

This is an UPPER BOUND — the proxy does not model SPAN netting
(30-40% relief for balanced short strangle) or position expiry
(margin releases as contracts expire). Real live binding rate is
meaningfully lower, but the direction is clear: **corsair is
margin-constrained at every Stage 1-4 cap level under this
strategy**. The operational question is not "if" margin binds
but "how often" — which only live data answers.

**Corsair book structure mitigates but doesn't eliminate margin
concerns**:
- Corsair quotes BOTH sides (C and P) and accumulates a MIX of
  long and short positions, not pure naked-short
- Resting book tends toward net-short gamma with roughly symmetric
  short-strangle-like structure around ATM
- SPAN nets offsetting positions: short strangle margin is roughly
  30-40% less than sum of naked legs
- Options ROLL as expiry approaches: theta decay reduces required
  margin over time; contracts expire and release capital
- Average margin usage is lower than peak margin usage

**Net effect on projections**: the Phase 4 numbers are
margin-UNCONSTRAINED. Live projections should be haircut for
margin binding explicitly (see §13.2 Base-case revision).

---

## 9. Stage 0 — pre-deployment gates (infrastructure readiness)

**Blocks Stage 1 start.** Every item must pass.

### 9.1 IBKR integration

- [ ] IBKR Gateway installed on deployment host, paper-account
      authenticated
- [ ] Market data subscription active for CME HXE options + HG
      futures (outright contracts, NOT inter-month spreads)
- [ ] Symbol filter in place: exclude spread contracts (symbol
      containing `-`) and UD-prefix multi-leg symbols
- [ ] Connection heartbeat + auto-reconnect logic; tested with
      deliberate gateway kill (should reconnect ≤ 60s)
- [ ] No orphaned orders across reconnect (verified via test run)

### 9.2 SABR stack readiness

- [ ] SABR calibration loop running against live HXE option
      quotes at 30s interval
- [ ] RMSE within 0.05 threshold on front-month contracts
      during normal market hours
- [ ] Forward (futures mid) source resolved per contract — use
      matching-expiry HG futures mid, NOT aggregated across
      contracts (lesson from crowsnest data bug; document
      per-contract futures source)
- [ ] `rv_shade_strength: 0.0` confirmed (no RV shading at this
      stage)

### 9.3 Kill switch live testing

Each Tier 1 kill must be deliberately triggered and verified
BEFORE Stage 1 begins:

- [ ] **Margin kill**: manually accumulate a position that brings
      margin to 50% of cap; confirm new fills blocked. Continue
      to 70%; confirm full halt. Verify cancel-all-resting fires.
- [ ] **Delta kill**: accumulate deliberate delta; confirm halt at
      ±5.0. Verify improving-fill exception allows reducing fills.
- [ ] **Theta kill**: short a sufficient premium to drive theta
      below −$500; confirm halt. Verify flatten-to-reduce logic.
- [ ] **Daily P&L kill**: simulate a series of losing trades to
      hit −$2,500; confirm halt for day.
- [ ] **|Vega| halt**: accumulate vega exposure; confirm halt at
      $1,000.

### 9.4 Margin-escape mode validation

- [ ] Deliberately exceed 50% margin, verify Tier-2 constraints
      suspend
- [ ] Verify Tier-1 kills remain binding during escape
- [ ] Verify fills are only allowed if they strictly reduce margin

### 9.5 Order management

- [ ] Order placement, modification, cancellation all verified
- [ ] Quote refresh cycle produces correct cancellations of stale
      orders
- [ ] No orphans across corsair restart (kill corsair process,
      restart, verify no stuck orders at IBKR)

### 9.6 Logging & data capture

- [ ] Every fill logged with full context (§17 schema)
- [ ] Every quote-skip event logged with reason code
- [ ] Every kill-switch trip logged with constraint value and
      precipitating action
- [ ] Every margin-escape event logged
- [ ] Log rotation / retention policy in place (recommend daily
      JSONL rotation, keep 90+ days)

### 9.7 Reconciliation integration (interface to crowsnest)

- [ ] Fill log written to a path crowsnest can read daily
      (recommend: `~/corsair/logs-paper/fills-YYYY-MM-DD.jsonl`)
- [ ] Fill log schema matches §17 below
- [ ] Daily EOD summary exported (JSON): fills, per-moneyness
      distribution, margin trajectory, kill-switch events

---

## 10. Stage 1 — smoke test ($100K margin, 1-2 weeks)

**Purpose**: operational validation, NOT profit. Expected
break-even to small loss.

### 10.1 Stage 1 parameters

| Parameter | Value |
|---|---|
| Margin cap | $100K |
| Total capital | $200K |
| DTE filter | ≤ 35 days |
| Strike scope | ATM ± 5 |
| Quote size | 1 lot |
| Hedging | OFF |
| min_edge | 0.001 |
| Expected fills/day | 20-30 (constraint-saturated at small cap) |
| Expected realized P&L | break-even to ±$125/day |
| Expected annualized | −$15K to +$30K |

### 10.2 Stage 1 success criteria (ALL must pass)

Operational validation:
- [ ] Order management clean: no orphaned orders, no reconciliation
      errors across any reconnect
- [ ] Every kill-switch trip logged with constraint value, tier
      label, triggering action
- [ ] Improving-fill logic verified by deliberately approaching
      a boundary
- [ ] Margin-escape mode fires on at least one natural occurrence
      and is correctly logged
- [ ] Kill switches never trip except in documented test cases
- [ ] Reconnection logic validated at least once deliberately
- [ ] Both sides (calls AND puts) getting fills; each ≥ 20% of
      total

Economic validation (Stage 1 floor — lower bar than Stage 2):
- [ ] Per-fill net revenue ≥ $60/fill on average
- [ ] Capture rate vs incumbent: ≥ 10%
- [ ] MtM tracking infrastructure works (most critical for Stage 2)

Crowsnest reconciliation:
- [ ] Paper fills ingested daily into `data/live_paper/`
- [ ] Daily divergence report produced (§19)
- [ ] Divergence on fills/day ≤ 50% on average over the 10-day
      window

### 10.3 Stage 1 failure modes (and what they mean)

- Operational failures (orphaned orders, kill switches tripping
  spuriously, reconnect failures) → stop, fix, restart Stage 1.
  Duration extends.
- Per-fill net < $60: investigate quote policy; possibly too
  aggressive on price or too wide on BBO filter. Tune and
  restart.
- Fills/day way below projection (< 10): investigate SABR fit
  quality, BBO width filter, or strike scope. Tune.
- Fills/day way above projection (> 50 at Stage 1 sizing):
  **unexpected; investigate** before advancing. Could indicate
  margin isn't binding as expected.

---

## 11. Stage 2 — validation scale ($250K margin, 2-3 weeks)

**Purpose**: first meaningful P&L. Validate that simulator-level
economics translate.

### 11.1 Stage 2 parameters

| Parameter | Value |
|---|---|
| Margin cap | $250K |
| Total capital | $500K |
| DTE filter | ≤ 35 (unchanged) |
| Strike scope | ATM ± 5 (unchanged) |
| Quote size | 1 lot (unchanged) |
| Hedging | OFF (unchanged) |
| min_edge | 0.001 (unchanged) |
| **Margin kill** | $350K (70% × $500K) |
| **Theta kill** | −$1,000 (2.5× Tier-2 floor) |
| **Daily P&L halt** | −$8,000 |
| **|Vega| halt** | $4,000 |
| **Tier-2 delta ceiling** | ±3.0 (unchanged; Stage 2-appropriate) |
| **Tier-2 theta floor** | −$400 |
| Expected fills/day | 55 (Phase 4 in-sample measured) |
| Expected realized P&L (3 weeks) | +$30K to +$60K |
| Expected annualized | +$500K to +$900K realistic; +$1.1M base case |

### 11.2 Stage 2 success criteria

All Stage 1 criteria still passing, PLUS:
- [ ] Live capture rate ≥ 8-15% (matches crowsnest projection)
- [ ] Live daily Sharpe ≥ 2.0 (after MtM included)
- [ ] Daily P&L positive on ≥ 60% of trading days
- [ ] Margin utilization < 130% of cap on average
- [ ] Mean |total delta| < 3.0
- [ ] Both calls AND puts each ≥ 30% of fills
- [ ] Margin-escape events reviewed: all are genuine recovery
      trades, not pathological

Crowsnest reconciliation gates:
- [ ] Divergence on fills/day ≤ 30% over rolling 5-day window
- [ ] Divergence on per-fill gross edge ≤ 30%
- [ ] Divergence on wrong-side % ≤ 25%

### 11.3 HG-specific watch items (not failure conditions, but flags)

- Per-strike concentration: if any single strike sees > 80%
  corsair share on any day, log and monitor. Phase 4 predicted
  13.9% of in-sample cells had > 50% per-strike penetration.
  If live frequency > 20%, investigate flow composition.
- Theta vs hedge MTM: the +$683/fill inventory MTM contribution
  is ~50-60% theta. Track daily; if negative-MTM days cluster,
  regime may be shifting.
- Realized vol vs in-sample: Phase 4 in-sample mean was 0.326
  annualized with std 0.136. If 5-day rolling rvol > 0.598 (2σ
  above), treat as regime-shift warning (not auto-halt).

### 11.4 Stage 2 graduation decisions

At end of 3-week Stage 2 window:

| Observed | Action |
|---|---|
| Sharpe > 3 sustained, gates pass | Consider advance to Stage 3 (requires separate decision memo) |
| Sharpe 2-3, gates pass | Hold at $250K another 2-3 weeks before scaling |
| Sharpe 1-2 | Do NOT scale; review parameters (min_edge, strike scope, etc.) |
| Sharpe < 1 | Pause; investigate; possibly scope-narrow to wings or ATM-only |
| Adverse tail: major DD, kill switch trips | Halt; full review before any restart |

**DO NOT auto-advance to Stage 3.** v4's Stage 3 spec was
ETH-targeted at $2M/$1M sizing with hedging-shadow-mode; HG
Stage 3 needs a fresh decision memo given what has been learned.

---

## 11b. Stage 1.5 — intermediate capital validation (ADDED v1.2)

**Purpose**: validate the strategy in a LESS constraint-saturated
regime before scaling to $250K. Distinguishes strategy-level
failure from margin-binding failure.

**Added because**: at $250K, post-hoc SPAN proxy shows corsair is
essentially always margin-bound (98.5% of fills would block at
upper-bound proxy; v1.1 measured 121-136% mean utilization).
Running Stage 2 at $250K directly confounds two different
failure modes. Stage 1.5 costs 1-2 weeks and gives clean
live-SPAN evidence before committing full Stage 2 capital.

### 11b.1 Stage 1.5 parameters

| Parameter | Value |
|---|---|
| Margin cap | **$150K** |
| Total capital | $300K |
| DTE filter | ≤ 35 (unchanged) |
| Strike scope | ATM ± 5 nickels (unchanged) |
| Quote size | 1 lot (unchanged) |
| Hedging | OFF (unchanged) |
| min_edge | 0.001 (unchanged) |
| **Margin kill** | $210K (70% × $300K) |
| **Theta kill** | −$750 |
| **Daily P&L halt** | −$5,000 |
| **|Vega| halt** | $2,500 |
| **Tier-2 delta ceiling** | ±3.0 |
| **Tier-2 theta floor** | −$300 |
| Expected fills/day | 35-50 (less constraint-saturated than Stage 1) |
| Expected realized P&L (2 wks) | +$10K to +$30K |
| Expected annualized | +$260K to +$780K |

### 11b.2 Stage 1.5 success criteria

All Stage 1 criteria still passing, PLUS:
- [ ] Live margin utilization measured; average < 100% of cap
      (indicates strategy fits $150K without binding >50% of the
      time)
- [ ] Capture rate ≥ 15% (less constrained than Stage 1's 10%)
- [ ] Per-fill net ≥ $200 (floor between Stage 1 $60 and
      Stage 2 target)
- [ ] Sharpe ≥ 1.5 daily

Crowsnest reconciliation gates:
- [ ] Divergence on fills/day ≤ 40%
- [ ] Live margin-utilization profile provides calibration for
      Stage 2 advance decision

### 11b.3 Stage 1.5 graduation to Stage 2

Advance to Stage 2 ($250K) only if:
- Stage 1.5 Sharpe ≥ 1.5 AND margin utilization < 100% average
- OR Stage 1.5 Sharpe ≥ 2 AND margin utilization 100-120% average
  (binding but manageable)

If Sharpe < 1.5 or utilization > 130% at $150K, **do NOT scale**.
Stage 1.5 failure at $150K means either the strategy itself
underperforms or the margin model is more binding than proxy
suggested — either way, scaling to $250K makes it worse.

## 12. Stage 3+ — deferred, NOT specified here

Decisions deferred until Stage 2 produces live data:
- Capital scale beyond $250K margin
- Hedging enablement (trigger level, throttle, cost model)
- DTE range expansion
- Strike range expansion beyond ATM ± 5
- Alternative min_edge curve

Stage 3 capital decision is a separate exercise that
incorporates: live Sharpe with σ bands tightened, per-strike AS
evidence, regime-stress context, and reconciliation variance.

---

## 13. Expected economics — by stage, with confidence bands

### 13.1 Stage 1 projection (2-week smoke test)

| Metric | Expected range | Rationale |
|---|---|---|
| Fills/day | 20-30 | Constraint-saturated at $100K margin |
| Per-fill net | $60-400 | $60 floor from v1.1; upper consistent with Phase 4 |
| Realized P&L window | −$1K to +$5K | Break-even target |
| Sharpe | N/A (too few days) | Statistical significance requires Stage 2+ |
| Operational validation | PASS/FAIL | Primary Stage 1 measurement |

### 13.2 Stage 2 projection (3-week validation)

Stage 2 is where economics actually measure. Confidence bands:

| Metric | Point estimate | ±1σ | ±2σ |
|---|---|---|---|
| Fills/day | 55 | ±15 | ±30 |
| Per-fill gross edge | $349 | ±$100 | ±$200 |
| Per-fill net (MTM incl.) | $400-700 | ±$200 | ±$400 |
| Window realized P&L | $45K | ±$25K | ±$50K |
| Window Sharpe (daily) | 2.5 | ±1.0 | ±2.0 |
| Window max DD | $20K | ±$15K | ±$40K |

Confidence-band derivation: Phase 4 in-sample Sharpe 7.10 is
a ceiling. Compression factors (regime normalization, in-sample
overfit, per-strike AS possibility, live-vs-sim divergence)
compound to ~40-75% reduction for base case. Base = 2.5, σ = 1.0
reflects dominance of regime + live-vs-sim uncertainty over
statistical SE.

### 13.3 Forward 12-month projection (assumes Stage 2 passes and steady-state)

**Revised v1.2 (2026-04-18)**: restricted-baseline backtest rerun
shows the nickel-strike + ATM±5 restriction compresses P&L from
$2.95M/yr to **$1.07M/yr** with Sharpe dropping from 7.10 to
6.53. The earlier −10% estimate was wrong; the restriction costs
~64% of P&L. Apply haircuts to the restricted baseline, not the
unrestricted one.

At $250K margin / $500K capital, sustained ~45-47 fills/day on
nickel-only strikes, with margin-binding haircut applied:

| Metric | Point | ±1σ | ±2σ |
|---|---|---|---|
| Annual P&L | **$400K** | ±$300K | ±$550K |
| IRR on $500K capital | **80%** | ±60% | ±110% |
| Annual Sharpe (blended regime) | **1.7** | ±1.0 | ±2.0 |
| Annual max drawdown | $30K-$70K | — | up to $150K in stress |

**95% CI:**
- Annual P&L: [−$150K, $950K]
- Annual Sharpe: [−0.3, +3.7]

Compression table (compounded, starting from RESTRICTED baseline
$1.07M/yr Sharpe 6.53):

| Factor | Compression | Notes |
|---|---|---|
| Regime normalization (63:41 → 50:50) | −30% | Vega leg of inventory MTM |
| In-sample overfit | −25% | Narrower restricted baseline; less overfit room |
| **Margin / SPAN binding** | **−20%** | Post-hoc proxy suggests live binding is common at $250K |
| Per-strike AS (nickel-filtered) | −5% | Lower than before — nickel filter already reduces per-strike concentration |
| Execution slippage | −5% | Simulator vs live |
| Cumulative (compounding) | **~60%** | Restricted baseline × 0.40 → base |

Base P&L: $1.07M × 0.40 ≈ **$430K**. Rounded to $400K with
uncertainty buffer.

### 13.3a Why the v1.1 base case (Sharpe 2.0, IRR 150%) was optimistic

Two mechanical issues in v1.1:
1. Applied haircuts to the UNRESTRICTED Phase 4 baseline (Sharpe 7.10
   / $2.95M/yr) without first applying the nickel+ATM±5 restriction
   that the spec itself mandates. Restricted baseline is $1.07M/yr —
   only 36% of unrestricted. The spec's own filter compresses the
   baseline materially.
2. The "−10% for nickel filter" estimate was based on
   volume-share heuristics (82% nickel volume); it missed that
   corsair's BACKTEST had disproportionately captured
   non-nickel-strike edge that disappears under the production
   filter.

v1.2 corrects both: start from the measured restricted backtest,
then apply remaining haircuts.

These are research projections. Live Stage 1-2 data tightens the
band; first σ-halving happens after ~5 weeks of live paper.

### 13.4 Scenario labels (v1.2 — restricted baseline + SPAN measured)

All numbers below are annual, at Stage 2 steady state, $500K capital.
Probabilities are priors for where live Stage 1-2 evidence will land.

- **Strong** (15%): margin binds rarely, regime stays calm,
  per-strike AS stays near nickel-filtered median (13.8%). Sharpe
  2.5-4, IRR 130-200%, annual ~$700-1000K.
- **Base** (50%): margin binds ~30% of the time, regime mixed.
  Sharpe 1-2, IRR 40-120%, annual ~$250-600K.
- **Conservative** (25%): margin binds heavily (50%+ of the time),
  capture rate drops, vega compresses MTM contribution. Sharpe
  0.3-1, IRR 15-50%, annual ~$100-250K.
- **Adverse** (10%): regime shift + margin binding + per-strike AS
  all compound. Sharpe < 0.3, IRR 0-20%, annual $0-$100K.
  Break-even worst case.

Probabilities unchanged from v1.1 — the v1.2 revision is to the
MAGNITUDE within each scenario (restricted baseline is materially
lower than unrestricted baseline). Strong case now caps below the
previous v1.1 Base case.

The operational priority remains: move probability mass from
Adverse toward Base via cheap live-data evidence during Stage 1-2.

---

## 14. Live data watch list — what to alert on

Derived from `config/live_divergence_alerts.yaml` in crowsnest:

| Metric | Simulator baseline | Alert threshold | Severity |
|---|---|---|---|
| Fills/day | 55.0 | ±30% (5-day avg) | PAGE |
| Per-fill gross edge USD | $349 | ±30% (5-day) | WARN |
| Per-fill net USD | $993 | ±40% (5-day) | WARN |
| Wrong-side % | 55% | ±25% (5-day) | PAGE |
| Max daily DD USD | $14,595 | ±50% (10-day) | PAGE |
| Per-strike concentration | — | any cell > 50% | WARN |
| HG realized vol (daily) | 0.326 mean | 2σ drift | PAGE |

**WARN** = log + daily review. **PAGE** = operator notification
within minutes.

---

## 15. Reconciliation protocol

### 15.1 Daily pipeline

```
Corsair side:
  ~/corsair/logs-paper/fills-YYYY-MM-DD.jsonl
  ~/corsair/logs-paper/daily-summary-YYYY-MM-DD.json

Crowsnest side:
  1. ~/crowsnest/scripts/ingest_paper_fills.py ingests
  2. Writes to ~/crowsnest/data/live_paper/fills_YYYY-MM-DD.parquet
  3. ~/crowsnest/scripts/reconcile_paper_vs_simulator.py runs
     at EOD
  4. Writes ~/crowsnest/data/live_paper/divergence_report.json
  5. Exit codes: 0 OK, 1 WARN, 2 PAGE
```

### 15.2 Weekly review cadence (during Stage 1-2)

End of each week:
- Crowsnest produces summary memo: live metrics, divergence
  report trend, any flagged events
- Reviewed jointly with corsair team
- Decisions documented if any parameter or kill threshold
  adjustment is warranted

---

## 16. Decision gates — summary

One-pager required at each boundary:

| Transition | Gate memo name | Content |
|---|---|---|
| Before Stage 1 | `stage0_complete_YYYY-MM-DD.md` | Gate 0 checklist all checked + sign-off |
| End of Stage 1 (2 wks) | `stage1_pass_fail_YYYY-MM-DD.md` | Pass/fail per §10.2 criteria |
| End of Stage 2 (5 wks) | `stage2_outcome_YYYY-MM-DD.md` | Stage 3 advance / hold / pause decision |

Each follows the `docs/findings/_TEMPLATE.md` structure, including
the REQUIRED "Boring re-derivation" section. For live data, the
boring re-derivation is "recompute the reported numbers from raw
fill log via a different aggregation method" — ensures reported
metrics aren't computed the same way twice.

---

## 17. Paper-trading fill log schema (corsair → crowsnest interface)

Expected format: JSONL (one JSON object per line), daily rotation.

### 17.1 Required fields per fill event

```json
{
  "ts": "2026-04-21T14:23:45.123Z",
  "event_type": "fill",
  "symbol": "HXEK6 C490",
  "side": "BUY",
  "size": 1,
  "price": 0.1150
}
```

Field semantics:
- `ts`: ISO 8601 UTC timestamp with millisecond precision
- `event_type`: `"fill"` for executions; `"skip"` for quote-skips
  (see §17.3)
- `symbol`: CME symbol, format `HXE[M][Y] [CP][strike]`
- `side`: **corsair's side** (not aggressor): `"BUY"` or `"SELL"`
- `size`: integer contracts
- `price`: fill price in $/lb (e.g., 0.1150 = $0.1150/lb)

### 17.2 Strongly recommended additional fields

```json
{
  "theo_at_quote": 0.1175,
  "quoter_count": 3,
  "incumbent_bid": 0.1140,
  "incumbent_ask": 0.1160,
  "corsair_bid": 0.1150,
  "corsair_ask": 0.1160,
  "margin_at_fill": 72500.0,
  "delta_at_fill": 1.2,
  "theta_at_fill": -150.0,
  "vega_at_fill": 32.5,
  "sabr_rmse_at_fill": 0.018,
  "forward_at_fill": 4.25
}
```

### 17.3 Skip-event schema

When corsair did NOT fill on a nearby aggressive trade:

```json
{
  "ts": "2026-04-21T14:24:02.500Z",
  "event_type": "skip",
  "symbol": "HXEK6 C490",
  "aggressor_side": "B",
  "aggressor_size": 2,
  "aggressor_price": 0.1155,
  "skip_reason": "wide_market",
  "market_bid": 0.1100,
  "market_ask": 0.1200
}
```

Skip reasons to log:
- `wide_market`: BBO > width filter
- `no_two_sided_market`: one side missing
- `below_min_edge`: theo-to-quote edge too thin
- `margin_ceiling`: blocked by Tier 1 margin
- `delta_kill`: blocked by Tier 1 delta
- `theta_kill`: blocked by Tier 1 theta
- `tier2_delta_ceiling`: blocked by Tier 2 delta ceiling
- `tier2_theta_floor`: blocked by Tier 2 theta floor
- `strike_not_calibrated`: SABR out of calibrated range
- `sabr_rmse_too_high`: SABR fit failed
- `dte_filter`: outside DTE ≤ 35
- `outside_atm_range`: outside ATM ± 5
- `quote_size_zero`: size constraint

### 17.4 Daily EOD summary schema

```json
{
  "date": "2026-04-21",
  "n_fills": 54,
  "n_skips": 142,
  "total_size": 54,
  "gross_edge_usd": 18900,
  "realized_pnl_usd": 35200,
  "max_margin_usd": 242000,
  "min_margin_usd": 98000,
  "kill_switch_trips": 0,
  "margin_escape_events": 2,
  "eod_delta": 0.4,
  "eod_theta": -280,
  "eod_vega": 180,
  "per_moneyness_fills": {
    "atm": 18,
    "near_otm_put": 14,
    "near_otm_call": 12,
    "deep_otm_put": 5,
    "deep_otm_call": 5
  }
}
```

---

## 18. Open questions / live-data watch

### 18.1 Quantifiable risks (the ones live data will directly resolve)

1. **Per-strike AS severity**: nickel-filtered median penetration
   is 13.8% (below the 15% MM-lit threshold at median); p90 is
   50%. Flow composition unknown — if hedger-dominated, AS stays
   mild; if informed-dominated, wrong-side % rises. Live
   wrong-side metric tracks this directly.

2. **Theta vs vega mix**: Phase 4 combined book showed 50-60%
   theta / 30-40% vega in the +$683/fill inventory MTM (before
   restricted-baseline compression). Live data in different vol
   regimes reveals whether front-only retains a theta-heavy
   profile or whether vega dominates in adverse regimes.

3. **Paper vs live fills divergence**: simulator projects 45-47
   fills/day (nickel-filtered); actual corsair fill rate may
   differ by ±30-50%. Stage 1.5 gives first measurement at
   moderate scale.

4. **SPAN binding empirical frequency**: proxy upper bound says
   98.5% at $250K, but real SPAN with netting may be 30-50%.
   Stage 1 ($100K) and Stage 1.5 ($150K) are specifically
   designed to measure this before Stage 2 commits.

5. **Regime sensitivity**: all Phase 4 data is Oct 2025 → Jan
   2026. A vol-expansion regime (e.g., Feb 2021 copper spike,
   May 2022 Ukraine shock) is NOT in sample. Regime-stress
   replay deferred; live regime monitoring is the practical
   substitute until then.

### 18.2 Known unknowns — not quantified in projections

Two rounds of findings have now lowered the base case (TAM
penetration correction, then strike restrictions + SPAN). The
pattern may not be complete. Remaining categories of potential
gap, each of which could move the base case further:

#### 18.2.1 Execution microstructure

- **Queue position at top-of-book**: corsair arrives at a strike
  as the Nth quoter. Real queue-priority dynamics (FIFO, pro-rata)
  affect actual fill share vs the simulator's 1/quoter_count
  heuristic. Could be ±50% on the per-strike capture assumption.
- **Quote refresh latency**: corsair's 30s refresh vs other MMs'
  potentially sub-second refresh. If corsair is consistently slow
  to reprice on underlying moves, fills land disproportionately
  on stale theos → wrong-side frequency rises.
- **Partial fills**: simulator assumes full fill at quoted size.
  Live IBKR execution can partial-fill, leaving corsair with
  residual exposure on cancelled rest.
- **Order-rejection paths**: IBKR rejects for margin, tier, risk,
  or session boundary reasons. Each rejection is a missed fill
  that doesn't exist in simulator.

**Severity estimate**: −10-20% to base case; not modeled.

#### 18.2.2 Post-trade dynamics

- **Post-fill book move**: when corsair buys/sells, the BBO often
  moves adversely (the fill itself revealed information). Real
  "adverse post-fill drift" ranges 20-50% of the quoted spread.
  Simulator doesn't model this — it assumes the mid stays at the
  theo post-fill.
- **Correlated fill clustering**: corsair's fills may cluster in
  time (all buyers arrive together on a vol move, then all
  sellers). Simulator treats fills as independent events for
  risk aggregation.
- **Exercise / assignment risk on short positions**: American-style
  HG options can be exercised early. Assignment before natural
  expiry creates a futures position corsair didn't plan for.
  Not modeled.

**Severity estimate**: −5-15% to base case; not modeled.

#### 18.2.3 Incumbent response

- **Tightening response**: the 1-4 existing HG quoters currently
  operate at a specific spread/size tradeoff. Corsair's entry at
  penny-jump pricing may cause them to tighten further or step
  aside. Tightening compresses per-fill edge; stepping aside
  increases corsair's per-strike penetration.
- **Anti-scalping**: sophisticated MMs may specifically adjust
  their quotes when they detect corsair's entries, reducing the
  edge available to corsair.
- **Size-matching**: incumbents may start matching corsair's
  1-lot size to split flow, reducing corsair's capture.

**Severity estimate**: unpredictable; could be +20% (if
incumbents step aside) or −30% (if incumbents compete hard).
Only live data reveals.

#### 18.2.4 Operational / infrastructure

- **IBKR gateway stability**: single-gateway dependency. Any
  extended outage (scheduled maintenance, data issues) costs
  quote-time.
- **Market-data latency**: IBKR market data may lag actual CME
  feed by 100-500ms. At 30s refresh this is minor, but during
  fast moves it's material.
- **Single-host deployment**: no failover. Host failure = full
  outage until manual intervention.
- **Time-zone / session-boundary edge cases**: daily break
  21:00-22:00 UTC; quote-cancellation and re-posting logic around
  boundary is operationally complex.

**Severity estimate**: availability impact only; doesn't affect
per-fill economics but reduces annualized realized P&L by
outage-fraction × expected run-rate.

#### 18.2.5 Regulatory / compliance

- **CFTC position limits**: HG copper has position limits under
  CFTC speculative-position rules. Corsair's scale-ceiling of
  148 fills/day doesn't plausibly hit them in steady state but
  accumulated open interest during positioning crunches could.
- **Reporting obligations**: Large Trader Reporting (LTR)
  thresholds may apply at certain scales. Corsair team should
  verify compliance posture before Stage 3+.

**Severity estimate**: not a Stage 1-2 concern; Stage 3+
verification item.

### 18.3 Disclosure posture

If any of §18.2 categories turn out to be material, the observed
base case in live data will deviate from projection in a
direction that this spec has NOT already haircut for. That's not
a failure of the projection — it's the purpose of live validation.
The spec is honest that the total compression from in-sample to
live is large and uncertain; §18.2 items could each add ±10-30%
in either direction.

Naming these explicitly means the spec is not surprised when
live differs — it's measured, not retrofitted.

---

## 19. Summary one-liner for hand-off

**HG front-only, CME HXE, DTE ≤ 35, ATM ± 5 NICKEL strikes
(multiples of $0.05) calls-and-puts, penny-jump quoting at
min_edge 0.001, no hedging at Stage 1-2, ramp $100K → $150K →
$250K margin over 6-8 weeks (Stage 1 smoke → Stage 1.5 SPAN
calibration → Stage 2 validation), TAM-enforced scaling ceiling
148 fills/day, restricted-baseline base case Sharpe 1.7 ± 1.0
and IRR 80% ± 60% on $500K capital at Stage 2 steady state.**

---

## 20. Reference artifacts (crowsnest side)

- `docs/findings/realism_upgrades/phase4_u1_as_recalibration_2026-04-18.md` — AS model calibration on clean data
- `docs/findings/realism_upgrades/phase4_u2_hedge_cost_2026-04-18.md` — hedge cost & per-expiry P&L decomposition
- `docs/findings/realism_upgrades/phase4_hg_front_only_headline_2026-04-18.md` — headline finding + scrutiny
- `docs/findings/hg_tam_analysis_2026-04-18.md` — TAM per rank + corrected per-(day, rank) penetration + per-strike addendum
- `docs/findings/realism_upgrades/eth_v4_spec_retraction_2026-04-18.md` — ETH retraction (for context on why HG only)
- `docs/findings/hg_front_only_deployment_prep_2026-04-18.md` — original deployment checklist (subset of this spec)
- `docs/findings/_TEMPLATE.md` — finding-doc template with required boring-re-derivation section (use for gate memos)
- `deployment/gate0_hg_front_only_prep.md` — Gate 0 checklist
- `scripts/ingest_paper_fills.py` — paper fill ingestion
- `scripts/reconcile_paper_vs_simulator.py` — daily reconciliation
- `config/live_divergence_alerts.yaml` — alert thresholds
- `data/live_paper/` — ingested normalized fills + daily divergence reports
- `data/backtest_archive_contaminated_2026-04-18/` — frozen pre-bug-fix parquets (for audit trail; not for deployment use)

---

## 21. Revision log

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-04-18 | Initial handoff spec |
| 1.1 | 2026-04-18 | Corrections: strike grid is $0.01 not $0.025; added nickel-strike filter; explicit margin-binding haircut; revised base case Sharpe 2.5 → 2.0 with ±1.2 σ; added §3.3a and §8.4 |
| **1.2** | 2026-04-18 | **Stage 1 lock v1.2**: (1) restricted-backtest rerun shows the spec's own filter compresses baseline P&L from $2.95M/yr to $1.07M/yr (Sharpe 7.10 → 6.53). Haircuts now compound from the restricted baseline; base case revised to Sharpe 1.7 ± 1.0, IRR 80% ± 60%, annual $400K ± $300K. (2) Post-hoc SPAN proxy on Phase 4 fills shows 98.5% of fills would block at $250K cap (upper bound; no SPAN netting). Added Stage 1.5 intermediate capital validation at $150K to distinguish strategy failure from margin binding before Stage 2. (3) Per-strike penetration re-measured on nickels only: p50 13.8% (was 20.0%), p90 50% (was 72.1%), cells >50% drops 13.9% → 8.3%. (4) Expanded §18 "Known unknowns" with execution microstructure, post-trade dynamics, incumbent response, operational/infrastructure, regulatory categories — each a potential gap not yet haircut for. |

---

## 22. Contacts & ownership

| Lane | Owner |
|---|---|
| Strategy / economics research | Crowsnest (bpemble@me.com) |
| Corsair execution & infrastructure | Corsair operator |
| Reconciliation interface | Joint (ingestion in crowsnest, fill log in corsair) |
| Stage 1-2 weekly review | Joint |
| Go/no-go Stage 3 decision | Joint, requires fresh decision memo |

End of document.
