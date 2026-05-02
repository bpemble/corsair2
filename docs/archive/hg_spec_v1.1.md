# Corsair — HG (Copper) Market Making Strategy Spec (v1.1)

## Version history

- **v1.0** (initial): `[ATM+$0.10, ATM+$0.50]` wings-only
- **v1.1** (this version): Updated inner bound to `$0.15` to skip the 10-15¢ adverse selection penalty zone. Same revenue, +21% Sharpe, -13% peak margin ratio.

## Executive summary

**Strategy**: Systematic options market making on HXE (COMEX Copper Option, American-style, on HG futures). Quote both calls and puts in moderately-OTM "wings" — skip the noisy near-ATM zone and focus on the $0.15-$0.50/lb range where adverse selection is manageable and flow is concentrated.

**Primary deployment config**: `[ATM+$0.15, ATM+$0.50]` calls and `[ATM-$0.50, ATM-$0.15]` puts, $250K margin cap, unhedged, flat constraints.

**Expected live performance** (with honest realistic discounts applied): **~$2,480/day, ~$625K/year, Sharpe ~2.0-2.3, on $400K total backing capital.**

---

## 1. Product specification

| Item | Value |
|---|---|
| Venue | CME Group (COMEX) |
| Product | HXE — Standard Copper Option |
| Exercise | American |
| Underlying | HG copper futures, front month by daily tick count |
| Contract size | 25,000 lbs of copper |
| Multiplier | $0.01/lb = $250/contract |
| Tick size | $0.0005/lb = $12.50/contract |
| Strike grid | 1¢ near ATM (primary), 5¢ moderate OTM, 25¢ deep OTM |
| Settlement | Physical (exercises into HG futures) |
| Databento parent symbol | `HXE.OPT` |
| Raw symbol format | `HXE{month}{year} {C/P}{strike_in_cents}` (e.g., `HXEK6 C450`) |

---

## 2. Strategy mechanics

### Quoting geometry

```
Call strikes quoted:  [ATM + $0.15/lb,  ATM + $0.50/lb]
Put strikes quoted:   [ATM - $0.50/lb,  ATM - $0.15/lb]
```

Where `ATM = round(F / 0.01) * 0.01` (nearest 1¢ strike to the front-month futures mid).

- At copper=$5.00, quote calls at strikes $5.15, $5.16, …, $5.50 (~35 strikes per side on the 1¢ grid)
- **Total active quotes**: ~35 calls + ~35 puts = ~70 quotes (needs to be refreshed as ATM moves)
- **Quote size**: 1 contract per strike (lot=1)

### Why the inner bound is $0.15 (not $0.10 or at ATM)

From the adverse selection measurement (scripts/hg_as_by_moneyness.py):

| Distance from ATM | n | AS/contract |
|---|---:|---:|
| within 2¢ | 1,758 | $17.62 |
| 2-5¢ | 1,872 | $29.32 |
| 5-10¢ | 3,254 | $14.03 |
| **10-15¢** | **2,727** | **$47.59** ⚠ |
| 15-25¢ | 5,485 | $11.82 |
| 25-50¢ | 9,977 | $12.81 |

The 10-15¢ bucket has a measured AS of $47.59/contract — nearly 4× the benign wing buckets. Shifting the inner bound from $0.10 to $0.15 surgically skips this penalty zone while preserving the full outer range.

### Tick-jumping strategy

- Post quotes at 1 tick inside the existing BBO when you're not already at TOB
- Goal: be at top of book, win the trade when it comes
- Expected capture rate: **30-50%** of eligible flow

### Min DTE filter

- Skip any expiry within **3 days** (settlement / pin risk avoidance)

### Side geometry

- **Both calls and puts simultaneously** (NOT calls-only like ETHUSDRR v2)
- HG has balanced flow; both sides work; puts do NOT have the gamma bleed problem ETH puts had

---

## 3. Risk constraints (flat triple-constraint)

Every fill must pass all three constraints. A fill is rejected if it would violate any of them AND is not "improving" toward recovery.

| Constraint | Value | Logic |
|---|---|---|
| **SPAN margin** | ≤ $250,000 | Fill blocked if would push `margin > cap` and `post_margin >= current_margin` |
| **Per-side delta (calls)** | `|net_call_delta|` ≤ 1.0 | Fill blocked if worsening drift beyond cap |
| **Per-side delta (puts)** | `|net_put_delta|` ≤ 1.0 | Same |
| **Net theta** | `net_theta >= -$100/day` | Fill blocked if worsening decay beyond cap |

**No hedging**. No futures hedge position. Delta management is via per-side caps only.

**No tiered constraints / margin escape mode.** Flat caps at every point.

### Why no hedging
Testing confirmed hedging:
- Triples peak margin (futures position adds to SPAN scan during fast moves)
- Cuts Sharpe in half in realistic mode
- Marginal revenue improvement doesn't justify the extra capital needed

### Why no tiered constraints
Testing confirmed tiered constraints:
- Reduce Sharpe by 1-3 points across every config tested
- The "escape fills" are adversely selected (they fire during margin breaches when the underlying has already moved)
- Net P&L drops 5-20% vs flat constraints

---

## 4. Capital staging plan

Start small, validate, scale up. Each stage has clear gate criteria.

### Stage 0 — Paper trading (2-3 weeks, $0 committed)
**Goal**: Measure your actual capture rate in HXE vs the backtest assumption.

**Setup**:
- Paper account on IBKR
- Run the full strategy logic with 1-lot quotes
- Log: all quotes, all fills, time-to-fill, competitor BBO movements
- Target: 200+ fills to get a statistically meaningful capture rate measurement

**Gate to Stage 1**:
- Capture rate ≥ 20% (vs backtest 30-50% assumption)
- No unexplained system errors for 3+ consecutive days
- Quote refresh latency < 500ms median
- Daily P&L tracking matches simulated expectation to within ±50%

### Stage 1 — Micro-deployment ($50K margin cap, ~$110K backing)
**Goal**: Prove live fills produce real P&L similar to backtest.

| Item | Value |
|---|---|
| Margin allowance | $50,000 |
| Total backing capital | $110,000 (2.2× buffer for peak margin p95) |
| Quote size | 1 contract |
| Expected daily P&L (backtest) | $307–$995/d (at 10-100% capture) |
| Expected live P&L | ~$230-$750/d after live discounts |
| Expected Sharpe (live) | ~1.5-2 |
| **Hard kill switch** | Peak margin > $150K OR daily loss > $5,000 |

**Gate to Stage 2**:
- 20 trading days of live operation
- Realized daily net P&L within 2σ of backtest prediction
- No margin breach triggering auto-liquidation

### Stage 2 — Moderate deployment ($100K margin cap, ~$205K backing)
| Item | Value |
|---|---|
| Margin allowance | $100,000 |
| Total backing capital | $205,000 (2.05× buffer) |
| Quote size | 1 contract |
| Expected daily P&L (backtest) | $768–$2,670/d (at 10-100% capture) |
| Expected live P&L | ~$575-$2,000/d |
| Expected Sharpe (live) | ~2-2.5 |
| **Hard kill switch** | Peak margin > $275K OR daily loss > $10,000 |

**Gate to Stage 3**:
- 40 trading days of live operation
- Cumulative PnL > $15,000
- No operational incidents (quote flood, connectivity failure, fat-finger)

### Stage 3 — Primary deployment ($250K margin cap, $400K backing)
**The main deployment target.**

| Item | Value |
|---|---|
| Margin allowance | $250,000 |
| Total backing capital | **$400,000** (1.6× buffer, conservative cushion) |
| Quote size | 1 contract |
| Expected daily P&L (backtest) | $1,566–$4,323/d (at 10-100% capture) |
| Expected live P&L | **~$2,480/d** (at 50% capture) |
| Expected annual P&L | **~$625K/year** |
| Expected Sharpe (live) | **~2.0-2.3** |
| **Hard kill switch** | Peak margin > $500K OR daily loss > $25,000 |

**Gate to Stage 4**:
- 60 trading days at Stage 3
- Cumulative PnL > $150K
- Max drawdown < $75K

### Stage 4 — Scale up (optional, $500K–$1M margin cap)
If Stages 1-3 validate the strategy, scale by multiples of $250K. The backtest showed limited additional benefit beyond $250K — revenue scales roughly linearly with capital but the peak margin p95 stays around $350K, so each additional $250K is mostly "dead capital" sitting in reserve.

---

## 5. Expected performance

### At the primary config ([+$0.15, +$0.50], $250K cap, unhedged)

| Capture | Fills/d | Net/d | Sharpe | pk/cap | pk95 | Annual | ROC |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10% | 11.3 | $1,566 | **8.71** | **1.1×** | 1.2× | $395K | 158% |
| 25% | 20.1 | $2,491 | 4.52 | 1.2× | 1.3× | $628K | 251% |
| 50% | 32.2 | $3,302 | 3.97 | 1.3× | 1.6× | $832K | 333% |
| 100% | 55.1 | $4,323 | **4.58** | **1.3×** | **1.5×** | $1.09M | **436%** |

**Realistic capture range for a tick-jumper in HXE: 30-50%**. Use the 25-50% rows as your planning base.

### Live estimate with honest discounts

Applied: Sharpe × 0.5 (missing gamma/vega in MtM model), mean × 0.75 (fill clustering + other second-order effects).

| Capture | Backtest net/d | **Live net/d** | Backtest Sharpe | **Live Sharpe** |
|---:|---:|---:|---:|---:|
| 25% | $2,491 | **~$1,870** | 4.52 | **~2.3** |
| **50%** | **$3,302** | **~$2,480** | 3.97 | **~2.0** |
| 100% | $4,323 | ~$3,240 | 4.58 | ~2.3 |

**Target live performance**: **$2,000-2,500/day**, **Sharpe 2-2.3**, **$500-625K/year** on $400K backing capital.

### What this feels like operationally

- **1 in 5 days** are negative
- **Worst day**: ~−$15,000 (backtest observed)
- **Worst week**: could be ~−$30K-$50K
- **Worst month**: could be ~−$75K-$100K in a volatile regime
- **Max backtest drawdown**: −$14,274

---

## 6. Strategy selection — the validated negatives

**These were tested and rejected. Do not reimplement.**

| Variant | Why rejected |
|---|---|
| Hedging with futures | Peak margin tripled, Sharpe cut in half |
| Tiered constraints (margin escape) | Adverse selection in escape fills dropped Sharpe 1-3 points |
| Very narrow wings `[+$0.01, +$0.10]` | Very low fill count, Sharpe roughly equivalent, less absolute $ |
| Very wide wings `[+$0.10, +$1.00]` | Theta exposure dominated, peak margin worse, Sharpe dropped |
| Calls-only (the ETH v2 approach) | HG doesn't have the gamma bleed problem; calls+puts is symmetric and wins on Sharpe |
| Inner bound at ATM `[ATM, +$0.50]` | Scores #1 on composite but exposes 2-5¢ AS penalty zone |
| Inner bound at $0.10 `[+$0.10, +$0.50]` | Still catches the 10-15¢ $47/ct AS penalty bucket |
| **Inner bound at $0.15** ★ | Skips penalty zone; same revenue as $0.10; +21% Sharpe; -13% peak margin |

---

## 7. Infrastructure requirements

### Data feeds
- Real-time HG futures mid (Databento `GLBX.MDP3` or IBKR live feed)
- Real-time HXE options BBO (same)
- Real-time SPAN margin estimate (broker-provided or compute from `src/synthetic_span.py`)
- Historical HG definitions (periodically refreshed) for strike list

### Execution
- IBKR API (gateway or TWS) for order management
- Rate limiting: respect exchange quote-per-second limits
- **Latency target**: quote update < 500ms from underlying tick
- **Fill handler**: immediately re-compute deltas, thetas, margin on every fill

### State tracking
- Per-position: strike, right, qty, entry IV, expiry
- Portfolio-wide: `running_delta_C`, `running_delta_P`, `running_theta`, current SPAN margin
- Daily aggregates: fills, spread capture, realized P&L, peak margin

### Software architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Market Data Subscriber               │
│  HG front-month futures → F                             │
│  HXE option BBO → per-strike bid/ask                    │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                   Strike Range Calculator               │
│  atm = round(F / 0.01) * 0.01                           │
│  call_strikes = [atm + 0.15 … atm + 0.50]  (1¢ steps)   │
│  put_strikes  = [atm - 0.50 … atm - 0.15]  (1¢ steps)   │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                   Quote Manager                         │
│  For each target strike:                                │
│    - Compute bid/ask = market_BBO ± 1 tick (tick-jump)  │
│    - Issue replace-order if our quote isn't TOB         │
│    - Cancel stale quotes outside new range              │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                   Fill Handler                          │
│  On fill:                                               │
│    - Update positions, compute new SPAN margin          │
│    - Pre-check next fill would breach any constraint    │
│    - If yes, pause quoting that side                    │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                   Risk Monitor                          │
│  Every 10s:                                             │
│    - Re-compute portfolio SPAN margin                   │
│    - If peak > $400K → hard stop, cancel all quotes     │
│    - If daily loss > $25K → hard stop                   │
│    - If Sortino < 3 over any 20-day rolling → alert     │
└─────────────────────────────────────────────────────────┘
```

---

## 8. Operational procedures

### Daily start-of-day (T-5 min before market open)
1. Connect to IBKR
2. Verify HG futures and HXE options market data flowing
3. Verify no overnight positions carrying unexpected risk
4. Reset daily counters (fills today, P&L today, hedge cost today)
5. Compute current ATM from futures mid
6. Compute target strike list (~35 calls + ~35 puts)
7. Begin quoting

### Intraday monitoring
- Quote health: >95% of target strikes should have active quotes in the book
- Fill rate: log fills/hour, compare to expected
- Margin utilization: display current SPAN margin vs $250K cap
- Sortino rolling 20-day (flag if < 3)

### End-of-day
1. Cancel all quotes 5 min before close
2. Snapshot positions, Greeks, margin
3. Log daily summary: fills, spread captured, theta, net P&L, peak margin
4. Check expiring positions (next-day DTE); alert if any will go to settlement

### Weekly
- Review realized vs expected: capture rate, per-fill economics, MtM drift
- Compare Sortino, max drawdown, worst-day to Stage N target thresholds
- Evaluate stage promotion/demotion criteria

### Monthly
- Re-calibrate SPAN scan parameters if broker margin observations diverge materially
- Re-measure AS on actual fills (not TBBO proxies) over the past 30 days
- Review gate metrics and decide if ready to scale

---

## 9. Parameter summary (copy-paste for code)

```yaml
# Product
product: HXE
multiplier: 25000  # lbs
strike_increment: 0.01  # $/lb (1¢ fine grid)
tick_size: 0.0005  # $/lb minimum price increment
default_iv: 0.25  # fallback when mid-solve fails

# Strategy geometry (v1.1 — primary change from v1.0)
quote_sides: [call, put]
call_inner_offset: 0.15  # $/lb above ATM  (v1.1; was 0.10 in v1.0)
call_outer_offset: 0.50  # $/lb above ATM
put_inner_offset: 0.15   # $/lb below ATM  (v1.1; was 0.10 in v1.0)
put_outer_offset: 0.50   # $/lb below ATM
quote_size: 1
min_dte: 3

# Risk constraints
max_delta_per_side: 1.0  # contract-delta (= 25,000 lbs notional)
max_theta_per_day: 100  # dollars, floor is -$100/day
margin_cap: 250000  # dollars (Stage 3; use 50K/100K for Stages 1/2)
total_backing_capital: 400000  # dollars

# Hedging
hedge_enabled: false
tiered_constraints: false

# SPAN calibration (single IBKR data point, refine on deployment)
span_up_scan_pct: 0.16
span_down_scan_pct: 0.16
span_vol_scan_pct: 0.25
span_extreme_mult: 3.0
span_extreme_cover: 0.33
span_short_option_min: 200.0

# Cost assumptions
commission_per_fill: 1.50  # dollars
# Adverse selection: use strike-specific lookup (see below)

# Kill switches
hard_stop_peak_margin: 400000  # dollars; close all and halt
hard_stop_daily_loss: 25000  # dollars
rolling_sortino_alert: 3  # over 20 trading days

# Capture rate expectation
expected_capture_rate_range: [0.30, 0.50]  # for tick-jumping in moderately-competed HXE
```

### Adverse selection lookup (strike-specific)

```python
# $/contract by |strike - F| in dollars/lb
AS_BY_DISTANCE = [
    (0.00, 0.02, 17.62),   # within 2¢ of ATM
    (0.02, 0.05, 29.32),   # 2-5¢ penalty zone
    (0.05, 0.10, 14.03),   # 5-10¢
    (0.10, 0.15, 47.59),   # 10-15¢ anomaly — v1.1 SKIPS THIS ZONE
    (0.15, 0.25, 11.82),   # 15-25¢ ← inner bound lives here
    (0.25, 0.50, 12.81),   # 25-50¢
    (0.50, 1.00, 11.55),   # 50-100¢ (not quoted)
    (1.00, 99.0, 10.61),   # >100¢ (not quoted)
]
# For the v1.1 wings [+$0.15, +$0.50], effective AS is ~$12/fill
# (mostly 15-25¢ and 25-50¢ buckets).
```

---

## 10. Model assumptions & known limitations

### What IS measured from data

| Assumption | Value | Source |
|---|---|---|
| Real trade direction (B/A) | Used verbatim | TBBO `side` column, 99.6% valid |
| Per-trade bid-ask spread | Used verbatim | TBBO `bid_px_00`, `ask_px_00` |
| Adverse selection by moneyness | Strike-specific lookup above | 34K 5-min markout pairs |
| Volume-weighted mean half-spread | $63.40/ct | Spread study |
| SPAN scan % | 16% (calibrated) | 1 IBKR data point ($18,379 ATM short call) |
| Median copper price | $5.52/lb | Oct 2025 – Mar 2026 |
| First-order MtM | Running delta × price change | Per-day boundary |

### What is NOT in the model (live P&L will differ)

| Omission | Direction | Severity |
|---|---|---|
| Gamma contribution to MtM | Adds variance, reduces Sharpe | **HIGH** — Sharpe discount ~×0.5 |
| Vega P&L from IV moves | Adds variance, reduces Sharpe | MEDIUM |
| Fill density correlated with adverse moves | Reduces mean | MEDIUM |
| Longer-window adverse selection (>5 min) | Reduces mean | MEDIUM |
| Queue priority / fill latency | Reduces capture rate | MEDIUM |
| SPAN calibration uncertainty | ±20% peak margin | LOW (bounded by hard stop) |

### Critical unverified parameters to measure during paper trading
1. **Actual capture rate** (backtest assumes 30-50%, reality unknown)
2. **Longer-horizon adverse selection** on actual held positions (30-min, 1-hour, EOD)
3. **True broker margin** vs the synthetic SPAN estimate
4. **Fill-clustering correlation** with underlying moves

---

## 11. Testing journey — decision log

This spec is the result of iterative testing. Summary of what was tried and what influenced the final decision:

| Test | Outcome | Impact on spec |
|---|---|---|
| ETH v2 baseline | Margin-bound, delta-bound, needed 50% util | Informed initial HG approach |
| HG spread study (median half-spread) | $25 ← wrong (used median) | Initial pessimistic estimate |
| HG spread study (volume-weighted mean) | $63.40 ← correct | Primary per-fill revenue |
| HG AS by moneyness | ATM has 2× AS of wings | Confirmed wings strategy hypothesis |
| Hedging experiments | Sharpe drops, peak margin triples | Hedging rejected |
| Tiered constraints | Sharpe drops 1-3 points | Tiered rejected |
| Wing width sweep (180 runs) | `[+$0.10, +$1.00]` scored high on absolute $ but low Sharpe | Narrower wings preferred |
| Wings-only sweep (478 runs) | `[ATM, +$0.50]` scored #1, wings-only close second | User chose wings-only for operational reasons |
| Multi-seed robustness | 5 seeds per config, path-dependence checked | Rankings are stable |
| **[+$0.15, +$0.50] vs [+$0.10, +$0.50] head-to-head** | **Same revenue, +21% Sharpe, −13% peak/cap** | **v1.1: updated primary to [+$0.15, +$0.50]** |

---

## 12. Suggested code layout for Claude Code

```
corsair/
├── strategies/
│   └── hg_wings_mm/
│       ├── __init__.py
│       ├── config.yaml          ← parameters from section 9
│       ├── strike_manager.py    ← strike range calc, quote refresh
│       ├── fill_handler.py      ← constraint checks, position updates
│       ├── risk_monitor.py      ← SPAN margin, kill switches
│       ├── signal.py            ← tick-jump logic
│       └── reporting.py         ← daily / weekly P&L reports
├── src/
│   └── synthetic_span.py        ← already exists, used for live SPAN estimate
├── data-market/
│   └── databento/               ← historical data for backtest comparison
└── scripts/
    ├── step2_triple_constraint_hg.py        ← backtest simulator (validated)
    ├── step2_hg_wings_only_sweep.py         ← the final sweep that produced this spec
    ├── hg_spread_study.py                   ← per-fill economics measurement
    ├── hg_as_by_moneyness.py                ← adverse selection measurement
    └── run_live_paper.py                    ← NEW: paper trading harness for Stage 0
```

### First implementation milestones
1. **Paper trading harness** (`run_live_paper.py`) — connect to IBKR paper account, implement quote refresh + fill handler + risk monitor. Target: Stage 0 ready in 1 week.
2. **SPAN live calibration** — run `src/synthetic_span.py` against live IBKR margin values every hour. Log delta between synthetic and broker. Target: tighten scan % calibration in Week 1.
3. **Dashboards** — daily P&L, fills, peak margin, rolling Sortino. Target: Week 2.
4. **Kill switches** — hard stops on peak margin and daily loss. Target: before Stage 1 deployment.
5. **Go-live checklist** — gate criteria from Stages 1-4 (paper-to-Stage-1 gate, etc.).
