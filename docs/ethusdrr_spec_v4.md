# Corsair v2 — ETHUSDRR Final Spec & Deployment Ramp (Revised v4)

## What's changing in this revision

One substantive update from v3:

1. **Constraint tiers**: All constraints are now classified as Tier 1 (hard) or Tier 2 (soft). Tier 2 constraints can be temporarily suspended during margin escape mode — when the system needs to close positions to reduce margin but the closing trade would violate a soft constraint.

**Motivation**: Observed 2026-04-09 — closing a short option reduced margin but tripped the theta floor, blocking the recovery trade. The tiered system prevents the constraint framework from wedging itself.

Everything else (product, capital, hedging, performance, ramp timeline) stays the same as v3.

---

## v5 status — REJECTED

A v5 variant was proposed that would abandon the 5 closest ATM strikes and quote only `[ATM+$125, ATM+$625]`. This was tested in the realistic backtest framework (real flow + per-fill spread + MtM proxy + strike-specific AS) and **rejected**.

**Why**: Unlike HG copper (where wings have MORE volume than ATM), ETH options flow concentrates at ATM. The wings-only strategy is flow-limited to ~3-5 fills/day at 100% theoretical capture. Estimated annual drop: ~92% from v4's target ($2.5M-$3.4M → $70K-$250K).

**Principle preserved**: Before applying "skip-the-ATM" logic to any product, verify (1) the wings have volume, and (2) the wings have meaningfully lower adverse selection. HG passes both. ETH fails both.

**ETHUSDRR stays on v4 (ATM-inclusive).**

---

## Final State (Production Target)

### Product

```
Product:           ETHUSDRR (CME ether options on futures)
Sides:             Calls AND puts (combined book, shared budgets)
Strike range:      Calls: ATM to ATM+20  ($500 above ATM)
                   Puts:  ATM-20 to ATM  ($500 below ATM)
Expirations:       Front 3 months (DTE > 3 AND DTE ≤ 105)
Quote size:        1 lot
Quote logic:       Penny-jump (1 tick inside incumbent BBO)
BBO width filter:  Skip if BBO > $10 wide (regime filter)
Inventory skew:    None
Hedging:           ENABLED
                   Instrument:   ETH futures (50 multiplier)
                   Trigger:      |total_delta| > 5.0
                                 (where total_delta = options Δ + futures qty)
                   Target:       Round options delta to nearest int,
                                 hedge to negate
                   Throttle:     Max once per 60 seconds
                   Cost model:   ~$27/contract per side
```

### Capital

```
Total capital:     $4,000,000
Margin cap:        $2,000,000  (50% utilization, shared C+P)
```

### Constraint tiers (combined portfolio, not per side)

**Tier 1 — Hard constraints (always binding, even in escape mode):**

| Constraint | Config key | Prod value | Gate logic |
|---|---|---|---|
| SPAN margin ceiling | `margin_ceiling_pct × capital` | $2,000,000 | Block if post > ceiling, unless improving (post ≤ current while current > ceiling) |
| Margin kill | `kill_switch.margin_kill_pct × capital` | $2,500,000 | Absolute hard stop — never exceeded |
| Delta kill | `kill_switch.delta_kill` | ±8.0 total, ±20.0 opt | Absolute hard stop |
| Theta kill | `kill_switch.theta_kill` | −$2,500 | Absolute hard stop |
| Long premium capital | `capital` | $4,000,000 | Cash outlay cap — improving-fill exception applies |

**Tier 2 — Soft constraints (can be temporarily breached during margin escape):**

| Constraint | Config key | Prod value | Gate logic |
|---|---|---|---|
| Net delta ceiling | `delta_ceiling` | ±10.0 opt | Block if `|post| > ceiling`, unless improving |
| Net theta floor | `theta_floor` | −$1,000 | Block if `post < floor`, unless improving |

### Margin escape mode

**Config**: `margin_escape_enabled: true`

When current margin is already above the margin ceiling:

- Any fill that strictly reduces margin (`post_margin < cur_margin`) is allowed
- Tier-2 soft constraints (delta ceiling, theta floor) are suspended — the fill can push them into breach
- Tier-1 hard kills (margin kill, delta kill, theta kill, long premium) remain binding — escape can't cross those
- Rationale: prevents the system from wedging when closing a position reduces margin but violates a tier-2 constraint (observed 2026-04-09 — closing a short tripped theta floor, blocking the recovery trade)

### Improving-fill exception (all constraints, strict mode)

Each constraint independently allows fills that move it in the right direction when already breached:

- Margin: `post ≤ current` while `current > ceiling`
- Delta: `|post| < |current|` while `|current| > ceiling`
- Theta: `post ≥ current` while `current < floor`

This applies at all times, independent of margin escape mode. The difference: improving-fill requires movement in the RIGHT direction for THAT constraint. Margin escape only requires margin improvement and suspends tier-2 checks entirely.

### Other kill switches (unchanged from v3)

```
|Net vega| > $15,000/vol point          → cancel all, halt
Daily P&L < -$25,000                    → cancel all, halt
IBKR Gateway disconnect                 → cancel all resting
```

These are operational kill switches outside the tier framework. They don't interact with margin escape or improving-fill logic.

### Hedging (Stage 4 only — unchanged from v3)

```
Enabled:           YES
Instrument:        ETH futures (50 multiplier)
Trigger:           |total_delta| > 5.0
Target:            Round options delta to nearest int, hedge to negate
Throttle:          Max once per minute
Granularity:       Integer contracts only
Slippage cost:     ~$27/contract per side
Expected:          ~2 hedges/day, ~$50-150/day cost
```

### Expected performance (unchanged from v3)

|  | Backtest | Realistic (after haircuts) |
|---|---|---|
| Annual P&L | $4.5M-$5.0M | $2.5M-$3.4M |
| ROC on $4M | 113-126% | 62-85% |
| Sharpe | 16-21 (single) | 6-9 (after MtM/AS) |
| Sortino | ~40-300 (single) | ~15-25 |
| Max drawdown | -$3K backtest | -$150K-$300K realistic |
| Fills/day | 73-97 | 50-80 realistic |
| Capture rate | 34-47% | 25-35% realistic |
| Mean theta | +$8K-12K/day | +$3K-7K/day realistic |
| Mean \|total delta\| | ~1.5 | ~1-3 (hedge keeps it near 0) |
| Hedge cost | ~$50/day | ~$50-150/day realistic |

---

## Deployment Ramp

The plan: start small to validate execution, scale up in stages as live data confirms each step. Each stage has explicit success criteria; failing them stops the ramp.

**Critical reframing**: Stage 1 is NOT a profit center. It's an insurance policy to validate the system before committing real capital. Expect break-even, not profit.

---

### Stage 1: Smoke Test ($100K margin / $200K capital)

**Purpose**: Validate end-to-end execution against real market data with minimal capital at risk. Not a profit-seeking deployment.

```
Capital:           $200,000
Margin cap:        $100,000
Sides:             Calls AND puts
DTE:               3-75 days (front 2 months)
Hedging:           OFF
```

**Tier 1 — Hard constraints:**

| Constraint | Stage 1 value | Gate logic |
|---|---|---|
| SPAN margin ceiling | 50% × $200K = $100,000 | Block if post > ceiling, unless improving (post ≤ current while current > ceiling) |
| Margin kill | 70% × $200K = $140,000 | Absolute hard stop — never exceeded |
| Delta kill | ±5.0 | Absolute hard stop |
| Theta kill | −$500 | Absolute hard stop |
| Long premium capital | $200,000 | Cash outlay cap — improving-fill exception applies |

**Tier 2 — Soft constraints:**

| Constraint | Stage 1 value | Gate logic |
|---|---|---|
| Net delta ceiling | ±3.0 | Block if `|post| > ceiling`, unless improving |
| Net theta floor | −$200 | Block if `post < floor`, unless improving |

**Margin escape**: ENABLED

**Other kill switches**:
- `|Net vega| > $1,000` → halt
- `Daily P&L < -$2,500` → halt

**Duration**: 1-2 weeks

**Realistic P&L expectation**: break-even to small loss/gain, −$50/day to +$125/day range. Annualized: −$15K to +$30K.

**Why so low**: At $100K margin, the strategy is constraint-saturated. Mean margin drift is ~145% of cap. Capture rate drops to ~10% because the portfolio fills up fast and rejects most trades.

**Success criteria** (must pass ALL to advance — none are P&L-based):
- ✅ Order management works without orphaned orders or reconciliation errors
- ✅ Constraints fire correctly (logged with reasons, including tier labels)
- ✅ Improving-fill logic works (test by deliberately approaching boundaries)
- ✅ Margin escape mode works (verify tier-2 suspension when margin > ceiling)
- ✅ Kill switches never trip (or trip on legitimate causes only)
- ✅ Per-fill economics: net revenue ≥ $60/fill
- ✅ Capture rate vs incumbent: ≥ 10%
- ✅ Reconnection logic works (test deliberately at least once)
- ✅ Both sides (calls and puts) are getting fills
- ✅ MtM tracking infrastructure works (most important)

**Cost of failure**: Approximately $1K-$5K total over the period.

---

### Stage 2: Validation Scale ($500K margin / $1M capital)

**Trigger**: Stage 1 passes after 1-2 weeks.

**Purpose**: Confirm the strategy produces meaningful fills and the constraint framework scales correctly.

```
Capital:           $1,000,000
Margin cap:        $500,000
DTE:               3-75 days (front 2 months)
Hedging:           OFF
```

**Tier 1 — Hard constraints:**

| Constraint | Stage 2 value |
|---|---|
| SPAN margin ceiling | 50% × $1M = $500,000 |
| Margin kill | 70% × $1M = $700,000 |
| Delta kill | ±5.0 |
| Theta kill | −$1,000 |
| Long premium capital | $1,000,000 |

**Tier 2 — Soft constraints:**

| Constraint | Stage 2 value |
|---|---|
| Net delta ceiling | ±3.0 |
| Net theta floor | −$400 |

**Margin escape**: ENABLED

**Other kill switches**:
- `|Net vega| > $4,000` → halt
- `Daily P&L < -$8,000` → halt

**Duration**: 2-3 weeks

**Realistic P&L expectation**: $150K-$280K annualized.

**Success criteria**:
- ✅ All Stage 1 criteria still passing
- ✅ Live capture rate: ≥ 8-15% (matches backtest)
- ✅ Live Sharpe: ≥ 2 (after MtM is included)
- ✅ Daily P&L: positive on average (can have losing days)
- ✅ Margin utilization stays < 130% on average
- ✅ Kill switches don't trip
- ✅ Mean delta drift < 5.0 (Tier-2 ceiling is ±3.0; some drift expected)
- ✅ Both sides contribute meaningfully (each at least 30% of fills)
- ✅ Margin escape events are logged and reviewed — confirm escapes were genuine recovery trades, not pathological

---

### Stage 3: Pre-Production Scale ($1M margin / $2M capital)

**Trigger**: Stage 2 passes after 2-3 weeks of clean operation.

**Purpose**: Approach production deployment. First meaningfully profitable stage. Validate hedging in shadow mode.

```
Capital:           $2,000,000
Margin cap:        $1,000,000
DTE:               3-105 days (front 3 months)
Hedging:           SHADOW MODE (compute hedges but don't execute)
```

**Tier 1 — Hard constraints:**

| Constraint | Stage 3 value |
|---|---|
| SPAN margin ceiling | 50% × $2M = $1,000,000 |
| Margin kill | 70% × $2M = $1,400,000 |
| Delta kill | ±8.0 |
| Theta kill | −$1,750 |
| Long premium capital | $2,000,000 |

**Tier 2 — Soft constraints:**

| Constraint | Stage 3 value |
|---|---|
| Net delta ceiling | ±5.0 |
| Net theta floor | −$700 |

**Margin escape**: ENABLED

**Other kill switches**:
- `|Net vega| > $8,000` → halt
- `Daily P&L < -$15,000` → halt

**Duration**: 2-3 weeks

**Realistic P&L expectation**: $410K-$535K annualized.

**Success criteria**:
- ✅ All Stage 2 criteria still passing
- ✅ Live ROC% trending toward 20-27% annualized
- ✅ Max drawdown over the period ≤ 5% of capital ($100K)
- ✅ Vega exposure stays within bounds
- ✅ Theta is consistently positive or near-zero (not bleeding > −$200/day)
- ✅ Mean delta drift < 8.0 (Tier-2 ceiling is ±5.0)
- ✅ Shadow hedging logic validates correctly
- ✅ Margin escape frequency is reasonable (not firing every cycle)

---

### Stage 4: Full Production ($2M margin / $4M capital)

**Trigger**: Stage 3 passes after 2-3 weeks. Including shadow-mode hedging validation.

**Purpose**: Full deployment at the spec'd target. Hedging enabled.

```
Capital:           $4,000,000
Margin cap:        $2,000,000
DTE:               3-105 days (front 3 months)
Hedging:           ENABLED
  Trigger:         |total_delta| > 5.0
  Target:          Round options delta to nearest int, hedge to negate
  Throttle:        Max once per 60 seconds
  Expected:        ~2 hedges/day, ~$50-150/day cost
```

**Tier 1 — Hard constraints:**

| Constraint | Stage 4 value |
|---|---|
| SPAN margin ceiling | 50% × $4M = $2,000,000 |
| Margin kill | 62.5% × $4M = $2,500,000 |
| Delta kill (total) | ±8.0 |
| Delta kill (options) | ±20.0 (catches hedge fail) |
| Theta kill | −$2,500 |
| Long premium capital | $4,000,000 |

**Tier 2 — Soft constraints:**

| Constraint | Stage 4 value |
|---|---|
| Net delta ceiling | ±10.0 (opt) |
| Net theta floor | −$1,000 |

**Margin escape**: ENABLED

**Other kill switches**:
- `|Net vega| > $15,000` → halt
- `Daily P&L < -$25,000` → halt
- IBKR Gateway disconnect → cancel all resting

**Duration**: Indefinite (this is the production state)

**Realistic P&L expectation**: $2.5M-$3.4M annualized.

**Ongoing monitoring (weekly)**:
- Capture rate
- Fills/day distribution
- Daily P&L distribution (spread / theta / MtM / hedge cost)
- Max drawdown over rolling 30-day window
- Net options delta / total delta / theta / vega exposure trends
- Constraint binding patterns (including tier-2 breach frequency)
- Margin escape event frequency and outcomes
- Mean |delta| drift relative to constraint
- Hedge frequency and total slippage cost
- Hedge effectiveness: ratio of mean |total delta| to mean |options delta|

**Re-evaluation triggers**:
- Sharpe drops below 3 over a 30-day window
- Max drawdown exceeds 10% of capital ($400K)
- Live capture rate < 20% sustained
- Per-fill economics drop below $60
- Mean |total delta| consistently > 5.0
- Hedge cost > $300/day sustained
- Margin escape fires > 10x/day sustained (constraint framework may be miscalibrated)
- ETH regime changes significantly (sustained 30%+ move)

---

## Stage Summary Table (v4)

| Stage | Margin ceil. | Capital | Tier-2 Δ / θ (soft, escapable) | Tier-1 kills Δ / θ / margin | DTE | Hedging | Duration | Backtest Annual | Realistic Annual | Realistic ROC |
|---|---|---|---|---|---|---|---|---|---|---|
| 1: Smoke Test | $100K | $200K | ±3.0 / −$200 | ±5.0 / −$500 / $140K | 3-75d | OFF | 1-2 wks | −$47K to +$31K | break-even | ~0% |
| 2: Validation | $500K | $1M | ±3.0 / −$400 | ±5.0 / −$1K / $700K | 3-75d | OFF | 2-3 wks | $331K-$560K | $150K-$280K | 15-28% |
| 3: Pre-prod | $1M | $2M | ±5.0 / −$700 | ±8.0 / −$1.75K / $1.4M | 3-105d | SHADOW | 2-3 wks | $824K-$1.07M | $410K-$535K | 21-27% |
| 4: Production | $2M | $4M | ±10.0* / −$1,000 | ±8.0† / −$2.5K / $2.5M | 3-105d | ON @ 5.0 | Ongoing | $4.5M-$5.0M | $2.5M-$3.4M | 62-85% |

\* Stage 4 Tier-2 delta is ±10.0 on options-only delta (looser because hedging keeps total delta near 0).
† Stage 4 delta kill has two thresholds: ±8.0 total delta AND ±20.0 options delta (catches hedge failure).

Margin escape is ENABLED at all stages.

---

## Constraint tier design rationale

### Why tiers exist

The original flat constraint model treated all constraints equally. This created a wedge scenario: when margin is above ceiling, the system needs to close positions to reduce margin. But closing a short option can reduce theta (making it more negative), tripping the theta floor and blocking the trade. The system becomes stuck — it can't reduce margin without violating theta, and it can't improve theta without adding margin.

The tier system resolves this by recognizing that constraints have different severity levels:

- **Tier 1 (hard)**: Existential risk limits. Margin kill prevents account blow-up. Delta/theta kills prevent catastrophic directional or decay exposure. These are never relaxed.
- **Tier 2 (soft)**: Portfolio quality targets. Delta ceiling and theta floor keep the book well-shaped under normal conditions. But they should yield when the system needs to escape a margin breach — a temporarily ugly book is better than a wedged system sitting at dangerous margin levels.

### Why margin escape only suspends Tier 2

Escape mode activates when margin > ceiling AND a fill would reduce margin. In this state, the fill is serving a critical risk-reduction purpose. The Tier-2 constraints are quality constraints, not safety constraints — it's acceptable to temporarily breach them to achieve a more important goal (margin reduction).

But Tier-1 kills remain binding because they represent absolute safety limits. Even during escape, you never want margin above the kill level, delta above the kill level, or theta below the kill level. These are the "never cross" lines.

### Interaction with improving-fill logic

Improving-fill and margin escape are independent mechanisms that can both allow a fill:

1. **Improving-fill**: "This fill moves constraint X in the right direction while X is already breached." Applies to ANY constraint, ANY time. Each constraint is evaluated independently.
2. **Margin escape**: "Margin is above ceiling and this fill reduces margin. Suspend Tier-2 checks entirely." Only applies when margin > ceiling.

A fill can be allowed by either mechanism. Examples:

- **Margin above ceiling, fill reduces margin AND worsens theta past floor**:
  - Allowed by margin escape (Tier-2 suspended)
  - Would NOT be allowed by improving-fill on theta (wrong direction)

- **Delta above ceiling, fill reduces delta but doesn't touch margin**:
  - Allowed by improving-fill on delta
  - NOT by margin escape (margin isn't above ceiling)

- **Margin above ceiling, fill reduces margin AND reduces delta breach**:
  - Allowed by BOTH mechanisms

---

## Total ramp time

- **Best case**: 6-8 weeks to full production.
- **More realistic**: 8-12 weeks with at least one stage requiring a second attempt or constraint adjustment.

---

## Critical notes (carried from v3, updated where noted)

### The smoke test is NOT a profit center

Stage 1 exists to validate the system. Stage 1 also validates the constraint tier framework and margin escape mode — this is critical because the wedge scenario can only be tested in a live environment with real margin calculations.

### Constraint drift is real and expected

At every stage, mean |delta| in the backtest exceeds the Tier-2 ceiling. This is because improving-fill logic accepts fills that reduce a breach. The Tier-1 kills catch truly extreme cases.

| Stage | Tier-2 Δ ceiling | Backtest mean |Δ| (low/high vol) |
|---|---|---|
| 1 | ±3.0 | 1.45 / 4.43 |
| 2 | ±3.0 | 3.46 / 5.02 |
| 3 | ±5.0 | 5.28 / 7.14 |
| 4 (with hedge @ 5.0) | ±10.0 options | opt: 24.4, total: 1.6 |

Note: With margin escape mode enabled, delta drift may be slightly higher than v3 backtest numbers because margin-reducing trades that breach delta ceiling are now allowed. This is acceptable — the Tier-1 delta kill (set 1.6-2.5x above the Tier-2 ceiling) is the true safety boundary.

### Margin drift is real and expected at smaller stages

| Stage | Margin cap | Backtest mean (low/high vol) |
|---|---|---|
| 1 | $100K | 145% / 151% |
| 2 | $500K | 121% / 136% |
| 3 | $1M | 98% / 106% |
| 4 | $2M | 83% / 96% |

With margin escape mode, Stages 1-2 (which are frequently above ceiling) will see more escape-mode fills. This is a feature: at small capital where the system is most constraint-saturated, escape mode prevents the worst wedge scenarios.

### Theta kill is new (added in v4)

v4 adds theta kill as a Tier-1 hard constraint. This was previously implicit (the system would just accumulate negative theta without a hard stop). The kill levels are set at 2.5x the Tier-2 floor:

| Stage | Tier-2 floor | Tier-1 kill |
|---|---|---|
| 1 | −$200 | −$500 |
| 2 | −$400 | −$1,000 |
| 3 | −$700 | −$1,750 |
| 4 | −$1,000 | −$2,500 |

The 2.5x ratio provides headroom for margin escape (which can push theta past the floor) while preventing runaway theta bleeding.

### Track MtM separately from spread+theta

At every stage, the daily P&L log should split:
- Spread capture: fills × $100/fill
- Theta: realized time decay
- MtM unrealized: change in option valuations day-over-day
- Hedge slippage: $0 in Stages 1-3, ~$50-150/day in Stage 4

### Calls AND puts from day 1

Symmetric delta confirmed at every scale.

---

## What changed from v3

| Item | v3 | v4 | Why |
|---|---|---|---|
| Constraint architecture | Flat (all equal) | Tiered (Tier 1 + Tier 2) | Prevents wedge when margin escape needs to breach a soft constraint |
| Margin escape mode | (not specified) | ENABLED at all stages | Observed 2026-04-09: closing a short tripped theta floor, blocking recovery trade |
| Theta kill switch | (none) | 2.5x theta floor | Safety net for margin escape — prevents runaway theta bleeding during escape mode |
| Delta/theta nomenclature | "constraints" | "Tier-2 soft constraints" | Clarifies that these yield during margin escape |
| Margin/kills nomenclature | "kill switches" | "Tier-1 hard constraints" | Clarifies that these never yield |
| Success criteria | (standard) | + margin escape validation + escape event monitoring | Stage 1 must validate that escape mode works. Stages 2+ must confirm escapes aren't pathological. |

Everything else (product, capital, hedging, performance numbers, ramp timeline, critical notes) stays the same as v3.

---

## FLAT (v3) vs TIERED (v4) — Comparison

| Stage | Mode | Fills/d | Net/d | Annual | Escape fills | Delta |
|---|---|---|---|---|---|---|
| 1: Smoke Test | FLAT | 6.0 | +$90 | +$23K | 0 | — |
| | TIERED | 6.8 | +$381 | +$96K | 133 | +$73K/yr |
| 2: Validation | FLAT | 28.5 | +$3,977 | +$1.00M | 0 | — |
| | TIERED | 28.7 | +$3,967 | +$1.00M | 24 | −$3K/yr (flat) |
| 3: Pre-prod | FLAT | 34.3 | +$6,685 | +$1.68M | 0 | — |
| | TIERED | 34.3 | +$6,685 | +$1.68M | 0 | $0 (identical) |
| 4: Production | FLAT | 35.9 | +$7,272 | +$1.83M | 0 | — |
| | TIERED | 35.7 | +$7,454 | +$1.88M | 0 | +$46K/yr |
