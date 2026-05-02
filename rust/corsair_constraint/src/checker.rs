//! `ConstraintChecker` — combined-budget gate logic.

use corsair_broker_api::Right;
use corsair_position::PortfolioState;
use serde::{Deserialize, Serialize};

use crate::gates::GateOutcome;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConstraintConfig {
    /// IBKR underlying symbol; bounds delta/theta gating to this
    /// product only.
    pub product: String,
    pub capital: f64,
    pub margin_ceiling_pct: f64,
    pub delta_ceiling: f64,
    /// Negative; lower bound on net theta.
    pub theta_floor: f64,
    /// Hard kill limits — final backstop even when margin-escape is on.
    pub margin_kill_pct: f64,
    pub delta_kill: f64,
    pub theta_kill: f64,
    /// Toggle for effective_delta_gating (CLAUDE.md §14). False
    /// reverts to options-only.
    pub effective_delta_gating: bool,
    /// Tier-1 margin priority escape — allows fills that strictly
    /// reduce margin even when they'd breach delta/theta soft caps,
    /// as long as no hard kill limit is crossed.
    pub margin_escape_enabled: bool,
}

impl ConstraintConfig {
    pub fn margin_ceiling(&self) -> f64 {
        self.capital * self.margin_ceiling_pct
    }
    pub fn margin_kill(&self) -> f64 {
        self.capital * self.margin_kill_pct
    }
}

/// Pre-fill check input. Caller computes:
/// - `cur_margin` and `post_margin` from corsair_pricing::span (with
///   IBKR scale already applied)
/// - `cur_long_premium` and `post_long_premium` for the solvency check
/// - `option_delta_per_contract` and `option_theta_per_contract` from
///   greek refresh; sign carries side direction
#[derive(Debug, Clone)]
pub struct ConstraintCheck {
    pub option_strike: f64,
    pub option_right: Right,
    pub option_expiry: chrono::NaiveDate,
    pub option_delta_per_contract: f64,
    pub option_theta_per_contract: f64,
    pub fill_qty_signed: i32,
    pub multiplier: f64,
    pub cur_margin: f64,
    pub post_margin: f64,
    pub cur_long_premium: f64,
    pub post_long_premium: f64,
    /// Hedge contract qty (signed), needed for effective_delta_gating.
    /// Caller passes 0 if no hedge or gating disabled.
    pub hedge_qty: i32,
}

pub struct ConstraintChecker {
    cfg: ConstraintConfig,
}

impl ConstraintChecker {
    pub fn new(cfg: ConstraintConfig) -> Self {
        Self { cfg }
    }

    pub fn config(&self) -> &ConstraintConfig {
        &self.cfg
    }

    /// Evaluate all gates against the proposed fill.
    pub fn check(&self, portfolio: &PortfolioState, c: &ConstraintCheck) -> GateOutcome {
        let cap = self.cfg.capital;
        let margin_ceiling = self.cfg.margin_ceiling();
        let margin_kill = self.cfg.margin_kill();

        // Pre-compute current state for this product.
        let cur_options_delta = portfolio.delta_for_product(&self.cfg.product);
        let cur_theta = portfolio.theta_for_product(&self.cfg.product);

        // Effective delta = options + hedge, when gating is on.
        let effective_hedge_qty = if self.cfg.effective_delta_gating {
            c.hedge_qty
        } else {
            0
        };
        let cur_delta = cur_options_delta + effective_hedge_qty as f64;
        let post_delta =
            cur_delta + c.option_delta_per_contract * (c.fill_qty_signed as f64);

        // Theta math.
        let option_theta_total = c.option_theta_per_contract * c.multiplier;
        let post_theta = cur_theta + option_theta_total * (c.fill_qty_signed as f64);

        // ── Tier-1 hard kill limits (always binding) ──────────────
        if c.post_margin > margin_kill {
            return GateOutcome::Rejected {
                reason: "margin_kill".into(),
                detail: format!(
                    "margin_kill: post=${:.0} > kill=${:.0}",
                    c.post_margin, margin_kill
                ),
            };
        }
        if post_delta.abs() > self.cfg.delta_kill {
            return GateOutcome::Rejected {
                reason: "delta_kill".into(),
                detail: format!(
                    "delta_kill: post={:+.2} > ±{:.1}",
                    post_delta, self.cfg.delta_kill
                ),
            };
        }
        if self.cfg.theta_kill < 0.0 && post_theta < self.cfg.theta_kill {
            return GateOutcome::Rejected {
                reason: "theta_kill".into(),
                detail: format!(
                    "theta_kill: post=${:.0} < ${:.0}",
                    post_theta, self.cfg.theta_kill
                ),
            };
        }

        // ── Tier-1 margin priority escape ─────────────────────────
        if self.cfg.margin_escape_enabled
            && c.cur_margin > margin_ceiling
            && c.post_margin < c.cur_margin
        {
            // Long-premium capital check is a solvency constraint
            // (cash outlay), not a risk metric — enforce it even in
            // escape mode, with the same improving-fill exception.
            if c.post_long_premium > cap && c.post_long_premium > c.cur_long_premium {
                return GateOutcome::Rejected {
                    reason: "long_premium".into(),
                    detail: format!(
                        "long_premium: post=${:.0} > cap=${:.0} (escape mode)",
                        c.post_long_premium, cap
                    ),
                };
            }
            return GateOutcome::AcceptedMarginEscape {
                detail: format!(
                    "margin escape: cur=${:.0} > ceiling=${:.0}, post=${:.0} reduces breach",
                    c.cur_margin, margin_ceiling, c.post_margin
                ),
            };
        }

        // ── Strict mode: per-constraint with improving-fill ────────

        // Margin
        if c.post_margin > margin_ceiling {
            // Improving exception: if currently breached, allow if post < cur.
            if c.cur_margin > margin_ceiling && c.post_margin >= c.cur_margin {
                return GateOutcome::Rejected {
                    reason: "margin".into(),
                    detail: format!(
                        "margin: post=${:.0} > ceiling=${:.0} (currently breached, fill not improving)",
                        c.post_margin, margin_ceiling
                    ),
                };
            }
            if c.cur_margin <= margin_ceiling {
                return GateOutcome::Rejected {
                    reason: "margin".into(),
                    detail: format!(
                        "margin: post=${:.0} > ceiling=${:.0}",
                        c.post_margin, margin_ceiling
                    ),
                };
            }
        }

        // Delta
        if post_delta.abs() > self.cfg.delta_ceiling {
            // Improving: if abs(post) < abs(cur), allow.
            if post_delta.abs() >= cur_delta.abs() {
                return GateOutcome::Rejected {
                    reason: "delta".into(),
                    detail: format!(
                        "delta: post={:+.2} > ±{:.1} (cur={:+.2})",
                        post_delta, self.cfg.delta_ceiling, cur_delta
                    ),
                };
            }
        }

        // Theta
        if post_theta < self.cfg.theta_floor {
            if post_theta <= cur_theta {
                return GateOutcome::Rejected {
                    reason: "theta".into(),
                    detail: format!(
                        "theta: post=${:.0} < ${:.0} (cur=${:.0})",
                        post_theta, self.cfg.theta_floor, cur_theta
                    ),
                };
            }
        }

        // Long-premium solvency
        if c.post_long_premium > cap && c.post_long_premium > c.cur_long_premium {
            return GateOutcome::Rejected {
                reason: "long_premium".into(),
                detail: format!(
                    "long_premium: post=${:.0} > cap=${:.0}",
                    c.post_long_premium, cap
                ),
            };
        }

        GateOutcome::Accepted
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use corsair_position::{ProductInfo, ProductRegistry};

    fn cfg() -> ConstraintConfig {
        ConstraintConfig {
            product: "HG".into(),
            capital: 200_000.0,
            margin_ceiling_pct: 0.50,    // → 100k
            delta_ceiling: 3.0,
            theta_floor: -200.0,
            margin_kill_pct: 0.70,        // → 140k
            delta_kill: 5.0,
            theta_kill: -500.0,
            effective_delta_gating: true,
            margin_escape_enabled: false,
        }
    }

    fn portfolio_empty() -> PortfolioState {
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        PortfolioState::new(r)
    }

    fn check(post_margin: f64, opt_delta: f64, opt_theta: f64, qty: i32) -> ConstraintCheck {
        ConstraintCheck {
            option_strike: 6.05,
            option_right: Right::Call,
            option_expiry: NaiveDate::from_ymd_opt(2026, 6, 26).unwrap(),
            option_delta_per_contract: opt_delta,
            option_theta_per_contract: opt_theta,
            fill_qty_signed: qty,
            multiplier: 25_000.0,
            cur_margin: 0.0,
            post_margin,
            cur_long_premium: 0.0,
            post_long_premium: 0.0,
            hedge_qty: 0,
        }
    }

    // ── Margin gate ────────────────────────────────────────────

    #[test]
    fn under_margin_ceiling_accepted() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let outcome = cc.check(&p, &check(50_000.0, 0.5, -0.001, 1));
        assert_eq!(outcome, GateOutcome::Accepted);
    }

    #[test]
    fn margin_breach_rejects() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let outcome = cc.check(&p, &check(110_000.0, 0.5, -0.001, 1));
        assert_eq!(outcome.rejection_reason(), Some("margin"));
    }

    #[test]
    fn margin_kill_takes_precedence() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let outcome = cc.check(&p, &check(150_000.0, 0.5, -0.001, 1));
        assert_eq!(outcome.rejection_reason(), Some("margin_kill"));
    }

    // ── Improving-fill exception ────────────────────────────────

    #[test]
    fn improving_margin_when_breached_accepted() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let mut c = check(105_000.0, 0.5, -0.001, -1); // sell, reducing
        c.cur_margin = 110_000.0; // currently breached
        let outcome = cc.check(&p, &c);
        // Margin breached but improving — accepted (no margin reject).
        assert!(matches!(outcome, GateOutcome::Accepted));
    }

    #[test]
    fn worsening_margin_when_breached_rejected() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let mut c = check(115_000.0, 0.5, -0.001, 1);
        c.cur_margin = 110_000.0;
        let outcome = cc.check(&p, &c);
        assert_eq!(outcome.rejection_reason(), Some("margin"));
    }

    // ── Delta gate ─────────────────────────────────────────────

    #[test]
    fn delta_breach_rejects_when_increasing() {
        let cc = ConstraintChecker::new(cfg());
        // Build portfolio with cur_delta = 2.5
        let mut r = ProductRegistry::new();
        r.register(ProductInfo {
            product: "HG".into(),
            multiplier: 25_000.0,
            default_iv: 0.30,
        });
        let mut p = PortfolioState::new(r);
        // Hand-set positions; simulating a 5-position book with
        // pre-computed greeks summing to 2.5 delta.
        use corsair_position::Position;
        use chrono::Utc;
        p.replace_positions(vec![Position {
            product: "HG".into(),
            strike: 6.05,
            expiry: NaiveDate::from_ymd_opt(2026, 6, 26).unwrap(),
            right: Right::Call,
            quantity: 5,
            avg_fill_price: 0.025,
            fill_time: Utc::now(),
            multiplier: 25_000.0,
            delta: 0.5,
            gamma: 0.0,
            theta: 0.0,
            vega: 0.0,
            current_price: 0.025,
        }]);
        let outcome = cc.check(&p, &check(50_000.0, 0.5, 0.0, 2));
        // cur=2.5; post=2.5+0.5*2=3.5; ceiling=3.0 → reject.
        assert_eq!(outcome.rejection_reason(), Some("delta"));
    }

    #[test]
    fn delta_kill_rejects() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let outcome = cc.check(&p, &check(50_000.0, 6.0, 0.0, 1));
        assert_eq!(outcome.rejection_reason(), Some("delta_kill"));
    }

    // ── Effective delta gating ─────────────────────────────────

    #[test]
    fn effective_gating_on_credits_hedge() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let mut c = check(50_000.0, 2.0, 0.0, 1);
        c.hedge_qty = -3; // short hedge cancels long delta
        let outcome = cc.check(&p, &c);
        // post_delta = 0 + 2*1 + (-3) = -1 → within ceiling → accept
        assert_eq!(outcome, GateOutcome::Accepted);
    }

    #[test]
    fn effective_gating_off_uses_options_only() {
        let mut cf = cfg();
        cf.effective_delta_gating = false;
        let cc = ConstraintChecker::new(cf);
        let p = portfolio_empty();
        let mut c = check(50_000.0, 4.0, 0.0, 1);
        c.hedge_qty = -10; // would offset under gating, but we're off
        let outcome = cc.check(&p, &c);
        // post_delta options-only = 4.0 → exceeds delta_kill 5.0? no.
        // exceeds ceiling 3.0 — yes. cur_delta=0, abs(post)=4 > abs(0).
        assert_eq!(outcome.rejection_reason(), Some("delta"));
    }

    // ── Theta gate ─────────────────────────────────────────────

    #[test]
    fn theta_breach_rejects() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        // theta_floor = -200, theta_kill = -500.
        // Use option_theta_per_contract = -0.012; multiplier = 25000
        // → option_theta_total = -300/contract.
        // Buying 1 → post_theta = -300, which breaches floor -200
        // but stays above kill -500 (so soft "theta" gate fires).
        let outcome = cc.check(&p, &check(50_000.0, 0.5, -0.012, 1));
        assert_eq!(outcome.rejection_reason(), Some("theta"));
    }

    // ── Long-premium solvency ──────────────────────────────────

    #[test]
    fn long_premium_breach_rejects() {
        let cc = ConstraintChecker::new(cfg());
        let p = portfolio_empty();
        let mut c = check(50_000.0, 0.5, 0.0, 1);
        c.post_long_premium = 250_000.0; // > capital 200k
        let outcome = cc.check(&p, &c);
        assert_eq!(outcome.rejection_reason(), Some("long_premium"));
    }

    // ── Margin escape ──────────────────────────────────────────

    #[test]
    fn margin_escape_accepts_improving_fill_with_soft_breach() {
        let mut cf = cfg();
        cf.margin_escape_enabled = true;
        let cc = ConstraintChecker::new(cf);
        let p = portfolio_empty();
        // Currently above ceiling; fill reduces margin AND breaches
        // delta soft cap (which would normally reject).
        let mut c = check(105_000.0, 4.0, 0.0, 1);
        c.cur_margin = 110_000.0;
        let outcome = cc.check(&p, &c);
        // post_delta = 4.0 → breaches ceiling 3.0 but escape on.
        // Should accept under escape (margin reducing).
        assert!(matches!(outcome, GateOutcome::AcceptedMarginEscape { .. }));
    }

    #[test]
    fn margin_escape_does_not_bypass_hard_kill() {
        let mut cf = cfg();
        cf.margin_escape_enabled = true;
        let cc = ConstraintChecker::new(cf);
        let p = portfolio_empty();
        let mut c = check(105_000.0, 6.0, 0.0, 1);
        c.cur_margin = 110_000.0;
        // post_delta=6 > delta_kill=5 — escape doesn't help.
        let outcome = cc.check(&p, &c);
        assert_eq!(outcome.rejection_reason(), Some("delta_kill"));
    }
}
