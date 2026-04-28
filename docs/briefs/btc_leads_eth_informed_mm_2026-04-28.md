# BTC-Leads-ETH Informed Market Making — Research Spec

**Status:** Draft, ready to hand to a research session on a separate
research server with backtest data access.
**Author context:** Spec written 2026-04-28 from a Corsair v1.4 operator
session. The receiving researcher has not seen that conversation — this
doc must stand alone.

## 1. Context

The operator runs Corsair, an HG copper options market-making system on
IBKR paper today. A future colocated execution venue (sub-1ms reaction
latency, FIX/CME direct) is being funded; this spec is preparation for
what to deploy on it.

The proposed strategy is **NOT** options market making. It is a directly
**informed market maker on CME ether futures (ETHUSDRR / front-month
contract, e.g. ETHK6)**, using **CME bitcoin futures (BRR / front-month,
e.g. BTCK6)** as a leading directional signal. The thesis is well-known
in crypto market-making: BTC futures are the price-discovery venue for
the broader crypto complex, and ETH typically follows BTC moves with a
small lag (commonly cited 50–500ms; empirical magnitude regime-dependent
and to be confirmed by this study).

The strategy's edge is **adverse-selection avoidance on ETH futures
takers, conditioned on BTC microstructure state**. We quote bid/ask
around a model fair value `F̂(ETH, t) = f(BTC_history, ETH_history)`,
not around ETH BBO. When BTC ticks up, our ask is already pulled up
before takers can hit our stale ask; when BTC ticks down, similarly on
the bid. Naive symmetric ETH MM gets adversely selected by takers with
BTC information; our model, in principle, doesn't.

This spec specifies the research project that decides whether this is
real and whether to build it. **Output is a decision artifact, not a
deployable model.**

## 2. Goals

1. **Phase 0 (kill/pass verdict, ~1 week):** Quantify the BTC→ETH
   lead-lag relationship on CME futures at sub-second timescales using
   live or historical tick data. Decide whether the signal is large
   enough at the operator's eventual reaction latency (assume <1ms) to
   exceed ETH futures spread + fees + adverse selection on signal
   failures.
2. **Phase 1 (model build, conditional on Phase 0 pass, ~2-4 weeks):**
   Build a conditional fair-value model for ETH given BTC microstructure
   state. Estimate the model's predictive power (R², hit rate of
   directional calls, MAE in basis points) at fixed horizons.
3. **Phase 2 (MM simulator, conditional on Phase 1 success, ~4-8
   weeks):** Build a tick-level MM simulator with realistic queue and
   adverse-selection modeling. Compare three policies: (a) symmetric
   naive MM, (b) defensive informed MM (widen on signal), (c)
   aggressive informed MM (skew quotes around F̂). Report P&L
   attribution and Sharpe at realistic capital and fee levels.
4. **One markdown report per phase** with a top-of-document verdict
   readable in under 60 seconds.

## 3. Non-goals

- No live integration with Corsair or the eventual colo system. The
  research output is decision artifacts and reference models, not
  production code.
- No order routing, no IBKR/FIX integration, no kill switches. Those
  are the colo build's problem, separate project.
- No deep-learning models in Phase 0/1. Linear (VECM) and gradient-
  boosted (LightGBM) baselines are the ceiling for this study. If
  deeper models are warranted, that's a Phase 3 decision.
- No multi-asset extensions (other crypto, DXY, equity index futures).
  BTC→ETH only. Adding cross-asset is a separate spec.

## 4. Data requirements

### 4.1 What the operator's environment can provide

The operator has a forward-capture probe running on the live IBKR
connection that produces JSONL rows for both ETHK6 and BTCK6 BBO
updates with local nanosecond + IBKR exchange timestamps. Format:

```
{"t_local_ns": <int>, "t_exch": <iso8601 or null>, "sym": "ETH"|"BTC",
 "bid": <float>, "ask": <float>, "bid_size": <int|null>,
 "ask_size": <int|null>, "contract": <localSymbol>}
```

Source: `scripts/dual_tick_capture.py`. ~1-3 ticks/sec combined,
appended to `logs-paper/dual_ticks-YYYY-MM-DD.jsonl` daily.

This is **sufficient for Phase 0** (10⁴–10⁵ tick events per day,
adequate for cross-correlation diagnostics) but **insufficient for
Phase 1+** because:
- Single-vendor (IBKR) feed with unknown latency calibration
- BBO only — no L2 depth, no trade prints, no queue dynamics
- One regime at a time (whatever the live market is doing)

### 4.2 What the research server should acquire

For Phase 1 and beyond, purchase historical tick data for CME crypto
futures. Recommended vendor: **Databento**. Required:

- **CME ether futures** front + 1 deferred month, full L2 depth +
  trade prints + BBO updates, microsecond exchange timestamps,
  ~12 months minimum (more if budget permits)
- **CME bitcoin futures**, same depth/timestamp/duration
- Both should be from MDP 3.0 or equivalent (CME's normalized feed),
  so timestamps are directly comparable

Estimated cost: $500–$3K depending on tier (CMBX/MBO depth vs MBP-1
vs BBO-only). Phase 1 minimally needs MBP-1 (top-of-book + sizes);
Phase 2 needs MBO (full order book) for queue modeling.

Storage: convert to Parquet on ingest. Raw JSONL/CSV is unwieldy past
~10⁷ rows.

### 4.3 Forward capture artifacts (existing)

A live capture is running as of this spec's date. Artifact location:
```
logs-paper/dual_ticks-2026-04-28.jsonl  (and subsequent dates)
```
Use these as a reality check — does the operator's IBKR feed look
similar in structure to the historical Databento data, modulo
timestamp granularity and queue information? If not, calibrate.

## 5. Phase 0 — Lead-lag empirical

### 5.1 Question

Given parallel BTC and ETH BBO tick streams, what is the cross-
correlation function of their log-returns at sub-second timescales,
and is BTC → ETH lead-lag (a) statistically significant, (b)
economically large enough to monetize given expected reaction latency?

### 5.2 Method

1. Load tick data into pandas. Construct mid-price series for each
   contract. Convert to log-returns at multiple resampling cadences:
   100ms, 500ms, 1s, 5s.
2. **Naive cross-correlation:** for each cadence, compute
   `corr(ΔlogP_ETH(t), ΔlogP_BTC(t-τ))` for τ ∈ [-2s, +2s] in 50ms
   steps. Plot. Identify peak τ* and peak ρ.
3. **Hayashi-Yoshida estimator:** apply directly to non-resampled
   tick returns to get an unbiased correlation estimate that handles
   asynchronous arrivals correctly. Naive resampling biases ρ toward
   zero on irregular high-freq data.
4. **Conditional response:** condition on BTC tick events of magnitude
   |ΔlogP_BTC| > k bps (k ∈ {1, 2, 5, 10}). For each, compute the
   distribution of ETH log-returns over the following 50ms, 200ms,
   500ms, 1000ms windows. Report mean, 25th/75th percentile, and
   hit rate (sign(ΔETH) = sign(ΔBTC)).
5. **Regime split:** repeat (4) split by:
   - BTC realized vol quartile (rolling 5-min vol of BTC log-returns)
   - Time of day (US hours / EU hours / Asian hours / weekend)
   - Calendar (across multiple weeks if data permits)
   Look for regime stability vs regime-conditional behavior.
6. **Information share (Hasbrouck or Gonzalo-Granger):** estimate the
   share of price discovery attributable to each market in a VECM
   framework. If BTC information share > 0.7, the leadership thesis
   is well-supported.

### 5.3 Pass criteria

Phase 0 passes if **all** of the following hold:
- CCF peak at positive lag (BTC leading) with peak ρ > 0.15 at the
  operator's reaction latency horizon (~1ms colo, conservatively
  evaluate at 50ms and 200ms anyway)
- Hayashi-Yoshida ρ at small lag ≥ 0.5 (unbiased estimate; the naive
  one will be smaller due to noise)
- Conditional hit rate > 60% on |ΔBTC| > 5bps events at 200ms horizon
- Regime stability: peak ρ does not flip sign or collapse to <0.05
  in any single regime split

If any of these fail decisively, **kill the project at Phase 0.**
Document the specific diagnostic that killed it.

### 5.4 Deliverable

Markdown report `phase0_lead_lag_verdict.md` with:
- Executive summary (3 sentences: lead-lag exists / doesn't, magnitude,
  go/no-go for Phase 1)
- CCF plots at each cadence
- Hayashi-Yoshida ρ table
- Conditional response distribution plots
- Regime split table
- Information share estimate

## 6. Phase 1 — Conditional fair value model

### 6.1 Question

Given BTC and ETH microstructure history up to time t, what is the
expected mid-price of ETH at time t+Δ (for Δ in {50ms, 200ms, 500ms,
1s, 5s})? Output is `F̂(t, Δ)` and a calibrated uncertainty σ_F̂(t, Δ).

### 6.2 Models to compare

1. **VECM baseline.** Vector error correction model on (logP_BTC,
   logP_ETH). Cointegration vector, error-correction coefficients,
   short-run VAR. Interpretable, regime-stable, low-risk-of-overfit.
   This is the baseline; everything else has to beat it.
2. **Kalman filter.** Hidden state = [logP_BTC_true, logP_ETH_true]
   with shared latent factor. Observation noise per market. Estimates
   F̂ and σ_F̂ jointly.
3. **LightGBM regression.** Tabular features: BTC return at multiple
   lags (1, 5, 10, 30 ticks back), BTC realized vol (rolling), order
   book imbalance (if Databento MBP available), trade flow imbalance
   (signed trade volume), time of day. Output: ETH log-return at
   horizon Δ.
4. **(Stretch) Mixture model:** GBM × regime classifier. Useful if
   Phase 0 reveals strong regime-conditional behavior.

### 6.3 Validation

**Walk-forward only — no k-fold, no random splits.** The relationship
is non-stationary; random splits leak information across regimes.

- Train on weeks 1–4, validate on weeks 5–6, test on weeks 7–8
- Roll forward by 2 weeks, repeat
- Report metrics on the *test* fold only:
  - Out-of-sample R² at each horizon
  - MAE in basis points
  - Directional hit rate
  - Calibration of σ_F̂ (does the realized residual distribution match
    the predicted variance?)

### 6.4 Pass criteria

Phase 1 passes if at least one model achieves, on test fold:
- OOS R² > 0.05 at 200ms horizon (yes, this is low — directional MM
  doesn't need high R², just better-than-spread predictability)
- Directional hit rate > 55% on the largest 10% of predicted moves
- σ_F̂ is well-calibrated (realized residual std ≈ predicted std,
  within 20%)

Do not optimize for R² alone. The MM simulator (Phase 2) is the actual
arbiter — Phase 1 just needs to show enough signal to make Phase 2
worth running.

### 6.5 Deliverable

Markdown report `phase1_fair_value_model.md` with model spec, training
config, walk-forward results table, and recommendation on which model
to carry into Phase 2.

## 7. Phase 2 — MM simulator with realistic fills

### 7.1 Question

Given a fair-value model from Phase 1, does an informed MM policy
produce risk-adjusted returns that beat (a) symmetric naive MM and
(b) idle (no quoting), after fees and adverse selection?

### 7.2 Simulator components

This is the part most retail stat-arb-MM projects underestimate.
**Mandatory components:**

1. **Queue position model.** From L2 depth snapshots and trade
   prints, simulate where in the queue our quote would sit when
   placed. Most ticks will not fill us; only certain (queue position
   at level + taker arrivals at level) combinations result in fills.
   This typically reduces effective fill rates by 5–10× vs naive
   "fill everything inside the spread" simulation.
2. **Adverse selection accounting.** When our quote does fill,
   compute the realized P&L over multiple horizons (10ms, 100ms, 1s,
   10s). The maker-side cost = adverse_pnl. The "edge" of an MM
   policy is `spread_captured - adverse_pnl - fees`.
3. **Realistic fee model.** ETH futures CME fees + clearing + IBKR
   pass-through (or future colo direct). Maker rebates if applicable.
   The colo target is presumably exchange-member rates.
4. **Inventory P&L.** Track running position; close-to-flat at end
   of day or via a configurable inventory penalty in the quote skew.
5. **Latency model.** Inject the operator's expected reaction
   latency (sweep: 100μs, 1ms, 10ms, 50ms) so we can show the
   sensitivity of P&L to colo speed.

### 7.3 Policies to compare

- **A. Idle:** no quoting. P&L = 0. Sanity check.
- **B. Symmetric MM:** quote bid = mid - half_spread, ask = mid +
  half_spread, no signal use. Adverse selection is unmitigated.
- **C. Defensive informed MM:** widen quotes proportional to |signal|
  or pull quotes entirely on |signal| > threshold. Asymmetric: pull
  the side facing the signal.
- **D. Aggressive informed MM:** quote bid/ask around F̂(t, Δ), not
  around ETH BBO. Aggressive skew on signal direction.

For each policy, sweep latency (100μs to 50ms) and report P&L,
Sharpe, max drawdown, fill count, average position size.

### 7.4 Pass criteria

Phase 2 passes if **policy D outperforms B by > 30% Sharpe at the
operator's expected production latency (1ms)** AND
**policy D's max-drawdown is < 3× policy B's max-drawdown.**

Defensive policy C is a fallback if D fails — it should at minimum
not underperform B (i.e., the signal can be used to *avoid losses*
even if not to capture aggressive edge).

### 7.5 Deliverable

Markdown report `phase2_mm_simulator.md` with simulator spec, fill
model validation, policy comparison tables and plots, latency
sensitivity sweep, and a final go/no-go recommendation on building
the production system.

## 8. Decision criteria summary

| Phase | Pass → next phase | Fail → action |
|---|---|---|
| 0 | Phase 1 budget approved | Kill project. Document why. |
| 1 | Phase 2 budget approved | Revisit features/data; potentially kill |
| 2 | Production build approved | Defensive policy may still be valuable |

**Total research budget if all phases pursued: 8–14 weeks.**
**Kill-early opportunity at Phase 0: ~1 week.**

## 9. Open questions for the research session

These are unresolved as of spec date and should be answered or
flagged during research:

1. **Latency calibration:** the operator's IBKR feed has unknown
   relative latency to the CME exchange. Compare timestamp deltas
   between IBKR and Databento on overlapping captures to estimate.
   This calibration matters for trusting the operator's forward-
   capture artifacts.
2. **ETH options interaction:** the operator currently runs an HG
   options MM but plans to extend to ETH options on the same colo
   stack. Should the ETH futures MM share inventory state with an
   eventual ETH options MM, or be siloed? Out of scope for this
   research but flag if the research surfaces relevant interactions
   (e.g., delta of options book is naturally a position in
   ETH-equivalent).
3. **BTC contract choice for the signal:** standard BTC futures
   (mult=5, full size) vs Micro Bitcoin (MBT, mult=0.1). Standard
   has tighter spreads, deeper book, better signal quality. Research
   should use whichever the historical data covers and note the
   implication for the colo build.
4. **Regime instability:** crypto markets had multiple structural
   shifts (post-FTX, post-ETF approval, halving cycles). If 12
   months of history is insufficient for regime variety, this is a
   risk for the deployed system. Flag explicitly in Phase 0 verdict.
5. **Information leak:** be paranoid about look-ahead bias in
   feature construction. Any feature involving "future" prices in
   the training set must be aligned with the model's prediction
   horizon. Walk-forward is the safety mechanism; preserve it.

## 10. Reference: forward-capture artifact format

The operator's IBKR forward capture writes JSONL rows to
`logs-paper/dual_ticks-YYYY-MM-DD.jsonl`. One row per unique
(bid, ask, bid_size, ask_size) change per symbol. Fields:

| Field | Type | Description |
|---|---|---|
| `t_local_ns` | int | `time.time_ns()` on capture host (UTC nanoseconds) |
| `t_exch` | string\|null | IBKR-exposed exchange timestamp (ISO8601 UTC) |
| `sym` | string | "ETH" or "BTC" |
| `bid` | float | Best bid price |
| `ask` | float | Best ask price |
| `bid_size` | int\|null | Best bid size in contracts |
| `ask_size` | int\|null | Best ask size in contracts |
| `contract` | string | IBKR localSymbol, e.g. "ETHK6", "BTCK6" |

Tick rate ~1-3 events/sec combined depending on market activity. A
session of ~8 hours produces ~50–100K rows / ~5–20 MB.

Use this for sanity checks against historical data structure and for
reality-checking lead-lag estimates against live market behavior.
