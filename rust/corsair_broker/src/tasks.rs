//! Tokio tasks. One task per broker stream; one per periodic timer.
//! All hold an `Arc<Runtime>` and acquire the relevant mutex.

use corsair_broker_api::{ConnectionState, OrderStatus};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast::error::RecvError;
use tokio::time::interval;

use crate::runtime::Runtime;

/// Spawn every task. Returns a Vec of join handles so the caller
/// can wait on shutdown.
pub fn spawn_all(runtime: Arc<Runtime>) -> Vec<tokio::task::JoinHandle<()>> {
    let mut handles = Vec::new();

    handles.push(tokio::spawn(pump_fills(runtime.clone())));
    handles.push(tokio::spawn(pump_status(runtime.clone())));
    handles.push(tokio::spawn(pump_ticks(runtime.clone())));
    handles.push(tokio::spawn(pump_errors(runtime.clone())));
    handles.push(tokio::spawn(pump_connection(runtime.clone())));

    handles.push(tokio::spawn(periodic_greek_refresh(runtime.clone())));
    handles.push(tokio::spawn(periodic_risk_check(runtime.clone())));
    handles.push(tokio::spawn(periodic_hedge(runtime.clone())));
    handles.push(tokio::spawn(periodic_snapshot(runtime.clone())));
    handles.push(tokio::spawn(periodic_account_poll(runtime.clone())));
    handles.push(tokio::spawn(daily_halt_rollover(runtime.clone())));

    handles
}

// ─── Stream pumps ──────────────────────────────────────────────────

async fn pump_fills(runtime: Arc<Runtime>) {
    let mut rx = {
        let b = runtime.broker.lock().await;
        b.subscribe_fills()
    };
    log::info!("pump_fills: subscribed");
    loop {
        match rx.recv().await {
            Ok(fill) => {
                handle_fill(&runtime, fill).await;
            }
            Err(RecvError::Lagged(n)) => {
                log::warn!("pump_fills: lagged {n} frames");
            }
            Err(RecvError::Closed) => {
                log::info!("pump_fills: channel closed; exiting");
                break;
            }
        }
    }
}

async fn handle_fill(runtime: &Arc<Runtime>, fill: corsair_broker_api::events::Fill) {
    // Try hedge first — if it accepts, the fill was a hedge fill, NOT
    // an option fill. (apply_broker_fill returns true only if the
    // instrument matches the hedge contract.)
    {
        let mut h = runtime.hedge.lock().unwrap();
        for m in 0..h.managers().len() {
            // Borrow checker: use for_product_mut keyed by symbol
            // is awkward in a loop; use index-by-position via a
            // helper. The Phase 3 fanout doesn't expose mut iter,
            // so we walk the products manually.
            let products: Vec<String> =
                h.managers().iter().map(|x| x.config().product.clone()).collect();
            let _ = m;
            for prod in &products {
                if let Some(mgr) = h.for_product_mut(prod) {
                    if mgr.apply_broker_fill(&fill) {
                        return;
                    }
                }
            }
            break;
        }
    }

    // Otherwise it's an option fill (or an unknown instrument we
    // ignore). Look up product from market data registry to find
    // strike/expiry/right. Wrap in a scope so the MutexGuard is
    // strictly released before any .await downstream — rustc's NLL
    // is conservative across await points.
    let instr = fill.instrument_id;
    let matched: Option<(
        String,
        f64,
        chrono::NaiveDate,
        corsair_broker_api::Right,
    )> = {
        let md = runtime.market_data.lock().unwrap();
        let mut found: Option<(String, f64, chrono::NaiveDate, corsair_broker_api::Right)> = None;
        for prod in runtime.portfolio.lock().unwrap().registry().products() {
            for t in md.options_for_product(&prod) {
                if t.instrument_id == Some(instr) {
                    found = Some((prod.clone(), t.strike, t.expiry, t.right));
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
        found
    };
    let (product, strike, expiry, right) = match matched {
        Some(v) => v,
        None => {
            log::debug!(
                "fill on unregistered instrument {} — ignoring (likely hedge contract not yet resolved)",
                instr
            );
            return;
        }
    };
    let qty_signed = match fill.side {
        corsair_broker_api::Side::Buy => fill.qty as i32,
        corsair_broker_api::Side::Sell => -(fill.qty as i32),
    };
    let outcome = {
        let mut p = runtime.portfolio.lock().unwrap();
        p.add_fill(&product, strike, expiry, right, qty_signed, fill.price, 0.0, 0.0)
    };
    log::warn!(
        "fill: {} {} {:?} {:+} @ {} → {:?}",
        product,
        strike,
        right,
        qty_signed,
        fill.price,
        outcome
    );

    // Per-fill daily P&L halt check. Mirrors
    // FillHandler.check_daily_pnl_only in Python.
    {
        let p = runtime.portfolio.lock().unwrap();
        let md = runtime.market_data.lock().unwrap();
        let mut r = runtime.risk.lock().unwrap();
        let _ = r.check_daily_pnl_only(&p, &*md);
    }

    // CLAUDE.md §10: rebalance hedge on every option fill (in
    // addition to the 30s periodic). Without this, every option fill
    // waits up to 30s for the periodic hedge tick — net delta drifts
    // unhedged for that window.
    let hedge_action = {
        let p = runtime.portfolio.lock().unwrap();
        let mut h = runtime.hedge.lock().unwrap();
        h.for_product_mut(&product)
            .map(|mgr| mgr.rebalance_on_fill(&p))
    };
    if let Some(corsair_hedge::HedgeAction::Place { is_buy, qty, reason, .. }) = hedge_action {
        if matches!(runtime.mode, crate::runtime::RuntimeMode::Live) {
            place_hedge_order(runtime, &product, is_buy, qty, &reason).await;
        } else {
            log::info!("hedge[{product}]: shadow Place {is_buy} qty={qty} ({reason})");
        }
    }
}

async fn pump_status(runtime: Arc<Runtime>) {
    let mut rx = {
        let b = runtime.broker.lock().await;
        b.subscribe_order_status()
    };
    log::info!("pump_status: subscribed");
    loop {
        match rx.recv().await {
            Ok(update) => {
                let mut oms = runtime.oms.lock().unwrap();
                let resolved = oms.apply_status(update.order_id, update.status);
                if !resolved {
                    log::debug!(
                        "status update for unknown orderId {}: {:?}",
                        update.order_id,
                        update.status
                    );
                }
                if matches!(
                    update.status,
                    OrderStatus::Filled | OrderStatus::Cancelled | OrderStatus::Rejected
                ) {
                    log::info!(
                        "order {} terminal: {:?} (filled={}, remaining={})",
                        update.order_id,
                        update.status,
                        update.filled_qty,
                        update.remaining_qty
                    );
                }
            }
            Err(RecvError::Lagged(n)) => log::warn!("pump_status: lagged {n}"),
            Err(RecvError::Closed) => break,
        }
    }
}

async fn pump_ticks(runtime: Arc<Runtime>) {
    let mut rx = {
        let b = runtime.broker.lock().await;
        b.subscribe_ticks_stream()
    };
    log::info!("pump_ticks: subscribed");
    loop {
        match rx.recv().await {
            Ok(tick) => {
                let mut md = runtime.market_data.lock().unwrap();
                use corsair_broker_api::TickKind;
                match tick.kind {
                    TickKind::Bid => {
                        if let Some(p) = tick.price {
                            md.update_bid(
                                tick.instrument_id,
                                p,
                                tick.size.unwrap_or(0),
                                tick.timestamp_ns,
                            );
                        }
                    }
                    TickKind::Ask => {
                        if let Some(p) = tick.price {
                            md.update_ask(
                                tick.instrument_id,
                                p,
                                tick.size.unwrap_or(0),
                                tick.timestamp_ns,
                            );
                        }
                    }
                    TickKind::Last => {
                        if let Some(p) = tick.price {
                            md.update_last(tick.instrument_id, p, tick.timestamp_ns);
                        }
                    }
                    _ => {} // BidSize/AskSize/Volume — handled implicitly via update_bid/ask args
                }
            }
            Err(RecvError::Lagged(n)) => log::warn!("pump_ticks: lagged {n}"),
            Err(RecvError::Closed) => break,
        }
    }
}

async fn pump_errors(runtime: Arc<Runtime>) {
    let mut rx = {
        let b = runtime.broker.lock().await;
        b.subscribe_errors()
    };
    log::info!("pump_errors: subscribed");
    loop {
        match rx.recv().await {
            Ok(err) => {
                log::warn!("broker error: {err}");
                // Phase 4.x: route certain protocol errors (e.g.
                // 1100 disconnect) to risk.fire as
                // KillSource::Disconnect. Today the connection
                // stream handles disconnects.
                let _ = runtime;
            }
            Err(RecvError::Lagged(_)) => {}
            Err(RecvError::Closed) => break,
        }
    }
}

async fn pump_connection(runtime: Arc<Runtime>) {
    let mut rx = {
        let b = runtime.broker.lock().await;
        b.subscribe_connection()
    };
    log::info!("pump_connection: subscribed");
    loop {
        match rx.recv().await {
            Ok(ev) => {
                log::warn!(
                    "connection event: {:?} {}",
                    ev.state,
                    ev.reason.as_deref().unwrap_or("")
                );
                // On reconnect, clear disconnect-source kills.
                if matches!(ev.state, ConnectionState::Connected) {
                    let mut r = runtime.risk.lock().unwrap();
                    let cleared = r.clear_disconnect_kill();
                    if cleared {
                        log::warn!("cleared disconnect-induced kill on reconnect");
                    }
                }
            }
            Err(RecvError::Lagged(_)) => {}
            Err(RecvError::Closed) => break,
        }
    }
}

// ─── Periodic tasks ──────────────────────────────────────────────

async fn periodic_greek_refresh(runtime: Arc<Runtime>) {
    let mut t = interval(Duration::from_secs(300)); // 5 min
    log::info!("periodic_greek_refresh: cadence 300s");
    loop {
        t.tick().await;
        let mut p = runtime.portfolio.lock().unwrap();
        let md = runtime.market_data.lock().unwrap();
        p.refresh_greeks(&*md);
    }
}

async fn periodic_risk_check(runtime: Arc<Runtime>) {
    let mut t = interval(Duration::from_secs(300));
    log::info!("periodic_risk_check: cadence 300s");
    loop {
        t.tick().await;
        let p = runtime.portfolio.lock().unwrap();
        let md = runtime.market_data.lock().unwrap();
        let mut r = runtime.risk.lock().unwrap();
        // Compute worst per-product Greeks from aggregate.
        let agg = p.aggregate();
        let (worst_delta, worst_theta, worst_vega) = worst_per_product(&agg, &runtime);
        // Pull cached IBKR maintenance margin for the kill gate
        // (CLAUDE.md §7). Falls back to 0 only if periodic_account_poll
        // hasn't run yet — first poll fires within 300s of boot.
        let margin_used = runtime
            .account
            .lock()
            .map(|a| a.maintenance_margin)
            .unwrap_or(0.0);
        let outcome = r.check(&p, margin_used, worst_delta, worst_theta, worst_vega, &*md);
        match &outcome {
            corsair_risk::RiskCheckOutcome::Killed(ev) => {
                log::error!("risk check fired kill: {ev:?}");
            }
            corsair_risk::RiskCheckOutcome::AlreadyKilled(_) => {}
            corsair_risk::RiskCheckOutcome::Healthy => {
                log::info!(
                    "RISK: positions={} long={} short={} delta={:+.2} theta={:+.0} vega={:+.0}",
                    agg.total.gross_positions,
                    agg.total.long_count,
                    agg.total.short_count,
                    worst_delta,
                    worst_theta,
                    worst_vega
                );
            }
        }
    }
}

/// Find the worst per-product Greeks. For delta we use absolute
/// magnitude (worst is largest |delta|); for theta, the most-negative
/// number; for vega, largest magnitude. Mirrors the per-product
/// loop in risk_monitor.py.
fn worst_per_product(
    agg: &corsair_position::aggregation::AggregateResult,
    runtime: &Arc<Runtime>,
) -> (f64, f64, f64) {
    let mut worst_delta = 0.0_f64;
    let mut worst_theta = 0.0_f64;
    let mut worst_vega = 0.0_f64;

    for (prod, g) in &agg.per_product {
        // Effective delta = options + hedge_qty when gating on.
        let hedge_qty = if runtime.config.constraints.effective_delta_gating {
            runtime.hedge.lock().unwrap().hedge_qty_for_product(prod)
        } else {
            0
        };
        let d = g.net_delta + hedge_qty as f64;
        if d.abs() > worst_delta.abs() {
            worst_delta = d;
        }
        if g.net_theta < worst_theta {
            worst_theta = g.net_theta;
        }
        if g.net_vega.abs() > worst_vega.abs() {
            worst_vega = g.net_vega;
        }
    }
    (worst_delta, worst_theta, worst_vega)
}

async fn periodic_hedge(runtime: Arc<Runtime>) {
    let mut t = interval(Duration::from_secs(30));
    log::info!("periodic_hedge: cadence 30s");
    let mut tick_count: u64 = 0;
    loop {
        t.tick().await;
        tick_count = tick_count.wrapping_add(1);

        // CLAUDE.md §10 periodic reconcile: every 4 ticks (~2 min)
        // call broker.positions() and reconcile hedge_qty against
        // IBKR's view. Catches divergences from non-filling IOCs.
        // Done every 4 ticks rather than every tick to keep the
        // periodic loop light — divergence accumulates slowly.
        if tick_count.is_multiple_of(4) {
            let positions_result = {
                let b = runtime.broker.lock().await;
                b.positions().await
            };
            if let Ok(positions) = positions_result {
                let mut h = runtime.hedge.lock().unwrap();
                for pos in positions {
                    if pos.contract.kind != corsair_broker_api::ContractKind::Future {
                        continue;
                    }
                    if let Some(mgr) = h.for_product_mut(&pos.contract.symbol) {
                        let mult = if pos.contract.multiplier > 0.0 {
                            pos.contract.multiplier
                        } else {
                            25_000.0
                        };
                        let avg = pos.avg_cost / mult;
                        mgr.reconcile_with_position(pos.quantity, avg, true);
                    }
                }
            }
        }

        // Take a snapshot of products + forwards while holding
        // market_data + portfolio briefly, then release before
        // hitting hedge.
        let products_and_forwards: Vec<(String, f64)> = {
            let p = runtime.portfolio.lock().unwrap();
            let md = runtime.market_data.lock().unwrap();
            p.registry()
                .products()
                .iter()
                .map(|prod| {
                    let f = md.underlying_price(prod).unwrap_or(0.0);
                    (prod.clone(), f)
                })
                .collect()
        };
        let now_ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);

        let actions: Vec<(String, corsair_hedge::HedgeAction)> = {
            let p = runtime.portfolio.lock().unwrap();
            let mut h = runtime.hedge.lock().unwrap();
            let mut out = Vec::new();
            for (prod, fwd) in &products_and_forwards {
                if let Some(mgr) = h.for_product_mut(prod) {
                    let action = mgr.rebalance_periodic(&p, *fwd, now_ns);
                    out.push((prod.clone(), action));
                }
            }
            out
        };
        for (prod, action) in actions {
            log::info!("hedge[{prod}]: {action:?}");
            if let corsair_hedge::HedgeAction::Place {
                is_buy,
                qty,
                reason,
                ..
            } = action
            {
                // Live mode only — shadow logs but doesn't place.
                if !matches!(runtime.mode, crate::runtime::RuntimeMode::Live) {
                    continue;
                }
                place_hedge_order(&runtime, &prod, is_buy, qty, &reason).await;
            }
        }
    }
}

/// Place a hedge order via the broker. Resolves the hedge contract
/// from the per-product manager's cached resolved contract; if no
/// contract is set, logs and skips.
async fn place_hedge_order(
    runtime: &Arc<Runtime>,
    product: &str,
    is_buy: bool,
    qty: u32,
    reason: &str,
) {
    let (contract_opt, ioc_offset, hedge_tick) = {
        let h = runtime.hedge.lock().unwrap();
        match h.for_product(product) {
            Some(mgr) => (
                mgr.hedge_contract().cloned(),
                mgr.config().ioc_tick_offset,
                mgr.config().hedge_tick_size,
            ),
            None => {
                log::warn!("hedge[{product}]: no manager registered, skipping");
                return;
            }
        }
    };
    let contract = match contract_opt {
        Some(c) => c,
        None => {
            log::warn!("hedge[{product}]: no hedge contract resolved yet, skipping");
            return;
        }
    };
    // IOC limit anchored at current underlying ± ioc_offset ticks.
    let f = {
        let md = runtime.market_data.lock().unwrap();
        md.underlying_price(product).unwrap_or(0.0)
    };
    if f <= 0.0 {
        log::warn!("hedge[{product}]: no underlying price, skipping");
        return;
    }
    let offset = (ioc_offset as f64) * hedge_tick;
    let lmt = if is_buy { f + offset } else { (f - offset).max(hedge_tick) };
    let req = corsair_broker_api::PlaceOrderReq {
        contract,
        side: if is_buy {
            corsair_broker_api::Side::Buy
        } else {
            corsair_broker_api::Side::Sell
        },
        qty,
        order_type: corsair_broker_api::OrderType::Limit,
        price: Some(lmt),
        tif: corsair_broker_api::TimeInForce::Ioc,
        gtd_until_utc: None,
        client_order_ref: format!("corsair_hedge_{}", reason),
        account: runtime
            .config
            .broker
            .ibkr
            .as_ref()
            .map(|i| i.account.clone()),
    };
    let result = {
        let b = runtime.broker.lock().await;
        b.place_order(req).await
    };
    match result {
        Ok(oid) => log::warn!(
            "hedge[{product}]: placed {} {} @ {:.4} oid={} ({})",
            if is_buy { "BUY" } else { "SELL" },
            qty,
            lmt,
            oid,
            reason
        ),
        Err(e) => log::error!("hedge[{product}]: place_order failed: {e}"),
    }
}

async fn periodic_snapshot(runtime: Arc<Runtime>) {
    let cadence = runtime.config.snapshot.cadence_ms;
    let mut t = interval(Duration::from_millis(cadence));
    log::info!("periodic_snapshot: cadence {cadence}ms");
    loop {
        t.tick().await;
        let result = {
            let p = runtime.portfolio.lock().unwrap();
            let r = runtime.risk.lock().unwrap();
            let h = runtime.hedge.lock().unwrap();
            let md = runtime.market_data.lock().unwrap();
            let mut s = runtime.snapshot.lock().unwrap();
            s.publish(
                &p,
                &r,
                &h,
                &*md,
                corsair_snapshot::payload::AccountSnapshot::default(),
            )
        };
        if let Err(e) = result {
            log::warn!("snapshot publish failed: {e}");
        }
    }
}

/// Session rollover at 17:00 CT — clears daily P&L halt (CLAUDE.md §8).
/// `RiskMonitor::clear_daily_halt` is a noop when no daily-halt kill is
/// active, so it's safe to fire every cycle. Implementation: tick every
/// 60s and check if the current US/Central time is 17:00..17:01.
async fn daily_halt_rollover(runtime: Arc<Runtime>) {
    use chrono::Timelike;
    let mut t = interval(Duration::from_secs(60));
    log::info!("daily_halt_rollover: armed; clears at 17:00 CT");
    let mut last_fired_day: Option<chrono::NaiveDate> = None;
    loop {
        t.tick().await;
        // chrono-tz isn't a workspace dep yet, so fall back to a fixed
        // CST offset (UTC-6). DST note: US/Central is UTC-5 (CDT) in
        // summer. We approximate at UTC-6 and accept ±1h drift across
        // DST transitions. TODO: depend on chrono-tz for true zone.
        let cst_offset = chrono::FixedOffset::west_opt(6 * 3600).unwrap();
        let now_cst = chrono::Utc::now().with_timezone(&cst_offset);
        if now_cst.hour() == 17 && now_cst.minute() == 0 {
            let today = now_cst.date_naive();
            if last_fired_day != Some(today) {
                let mut r = runtime.risk.lock().unwrap();
                if r.clear_daily_halt() {
                    log::warn!("daily_halt_rollover: cleared daily halt at 17:00 CT");
                }
                last_fired_day = Some(today);
            }
        }
    }
}

async fn periodic_account_poll(runtime: Arc<Runtime>) {
    let mut t = interval(Duration::from_secs(300));
    log::info!("periodic_account_poll: cadence 300s");
    loop {
        t.tick().await;
        let result = {
            let b = runtime.broker.lock().await;
            b.account_values().await
        };
        match result {
            Ok(snap) => {
                log::info!(
                    "ACCOUNT: NLV=${:.0} maint=${:.0} init=${:.0} BP=${:.0} realized=${:.0}",
                    snap.net_liquidation,
                    snap.maintenance_margin,
                    snap.initial_margin,
                    snap.buying_power,
                    snap.realized_pnl_today
                );
                let ibkr_actual = snap.maintenance_margin;
                if let Ok(mut a) = runtime.account.lock() {
                    *a = snap;
                }
                // CLAUDE.md §3: ibkr_scale calibration. Compute the
                // current raw synthetic SPAN against current positions
                // and forward, then divide IBKR's MaintMarginReq by it.
                if ibkr_actual > 0.0 {
                    let raw = compute_raw_synthetic_margin(&runtime);
                    if raw > 0.0 {
                        let mut cc = runtime.constraint.lock().unwrap();
                        cc.update_cached_margin(ibkr_actual, raw, now_ns());
                    }
                }
            }
            Err(e) => log::warn!("account_values poll failed: {e}"),
        }
    }
}

fn now_ns() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

/// Compute raw synthetic SPAN margin for the current portfolio.
/// Used by `periodic_account_poll` to recalibrate the ibkr_scale.
/// Returns 0 when there are no positions, no underlying price, or no
/// per-product config — caller treats those as "no calibration this
/// cycle".
fn compute_raw_synthetic_margin(runtime: &Arc<Runtime>) -> f64 {
    use chrono::Utc;
    let p = runtime.portfolio.lock().unwrap();
    let md = runtime.market_data.lock().unwrap();
    if p.position_count() == 0 {
        return 0.0;
    }

    // Group by product: each product has its own multiplier and
    // forward; SPAN's portfolio_margin assumes a single multiplier.
    let mut by_product: std::collections::HashMap<String, Vec<(f64, char, f64, f64, i64)>> =
        Default::default();
    let now = Utc::now();
    for pos in p.positions() {
        let t = (pos.expiry.and_hms_opt(16, 0, 0).unwrap()
            .and_utc()
            - now)
            .num_seconds() as f64
            / (365.0 * 86400.0);
        if t <= 0.0 {
            continue;
        }
        let right_char = match pos.right {
            corsair_broker_api::Right::Call => 'C',
            corsair_broker_api::Right::Put => 'P',
        };
        // IV: prefer market-implied via greek refresh. We don't have
        // the per-strike IV cached cheaply here, so fall back to
        // product default_iv. The scale calibration is forgiving —
        // raw_synthetic doesn't need to be exact, just stable.
        let iv = p
            .registry()
            .get(&pos.product)
            .map(|i| i.default_iv)
            .unwrap_or(0.30);
        by_product
            .entry(pos.product.clone())
            .or_default()
            .push((pos.strike, right_char, t, iv, pos.quantity as i64));
    }

    let mut total = 0.0;
    for (product, positions) in by_product {
        let f = match md.underlying_price(&product) {
            Some(f) => f,
            None => continue,
        };
        let info = match p.registry().get(&product) {
            Some(i) => i,
            None => continue,
        };
        let cfg = corsair_pricing::span::SpanConfig {
            up_scan_pct: 0.05,
            down_scan_pct: 0.05,
            vol_scan_pct: 0.25,
            extreme_mult: 2.0,
            extreme_cover: 0.35,
            short_option_minimum: 50.0,
            multiplier: info.multiplier,
        };
        let m = corsair_pricing::span::portfolio_margin(f, &positions, &cfg);
        total += m.total_margin;
    }
    total
}
