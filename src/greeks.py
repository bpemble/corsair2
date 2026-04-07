"""Black-76 futures options Greeks calculator.

Reused from Corsair v1. Computes delta, gamma, theta, and vega
for individual options and aggregated portfolios.
"""

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


@dataclass
class Greeks:
    """Per-contract Greeks for a single option."""
    delta: float       # Per-contract delta
    gamma: float       # Per-contract gamma (dollar-denominated)
    theta: float       # Per-contract daily theta (dollars)
    vega: float        # Per-contract vega per 1% vol move (dollars)
    iv: float          # Implied volatility used


class GreeksCalculator:
    """Black-76 Greeks calculator for futures options."""

    @staticmethod
    def _d1(F: float, K: float, T: float, sigma: float) -> float:
        return (np.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * np.sqrt(T))

    @staticmethod
    def _d2(d1: float, sigma: float, T: float) -> float:
        return d1 - sigma * np.sqrt(T)

    def calculate(
        self,
        F: float, K: float, T: float, sigma: float,
        r: float = 0.0, right: str = "C", multiplier: float = 50,
    ) -> Greeks:
        """Calculate all Greeks for a single option using Black-76.

        Args:
            F: Futures price (underlying).
            K: Strike price.
            T: Time to expiration in years.
            sigma: Implied volatility (annualised).
            r: Risk-free rate.
            right: 'C' for call, 'P' for put.
            multiplier: Contract multiplier for dollar-denominated Greeks.
        """
        is_call = right.upper() == "C"

        if T < 1e-10 or sigma <= 0 or F <= 0 or K <= 0:
            intrinsic_delta = 1.0 if F > K else (0.0 if F < K else 0.5)
            if not is_call:
                intrinsic_delta -= 1.0
            return Greeks(delta=intrinsic_delta, gamma=0.0, theta=0.0, vega=0.0, iv=sigma)

        discount = np.exp(-r * T)
        sqrt_T = np.sqrt(T)

        d1 = self._d1(F, K, T, sigma)
        d2 = self._d2(d1, sigma, T)

        n_d1 = norm.pdf(d1)
        N_d1 = norm.cdf(d1)
        N_d2 = norm.cdf(d2)

        # Delta
        if is_call:
            delta = discount * N_d1
        else:
            delta = discount * (N_d1 - 1.0)

        # Gamma (dollar-denominated)
        gamma = discount * n_d1 / (F * sigma * sqrt_T) * multiplier

        # Theta (per calendar day, dollar-denominated)
        common_term = -discount * F * n_d1 * sigma / (2.0 * sqrt_T)
        if is_call:
            theta = (common_term - r * K * discount * N_d2) / 365.0
        else:
            theta = (common_term + r * K * discount * norm.cdf(-d2)) / 365.0
        theta_dollar = theta * multiplier

        # Vega (per 1% vol move, dollar-denominated)
        vega = F * discount * n_d1 * sqrt_T / 100.0
        vega_dollar = vega * multiplier

        # Guard against NaN/inf
        if not (np.isfinite(delta) and np.isfinite(gamma)
                and np.isfinite(theta_dollar) and np.isfinite(vega_dollar)):
            intrinsic_delta = 1.0 if F > K else (0.0 if F < K else 0.5)
            if not is_call:
                intrinsic_delta -= 1.0
            return Greeks(delta=intrinsic_delta, gamma=0.0, theta=0.0, vega=0.0, iv=sigma)

        return Greeks(
            delta=delta, gamma=gamma, theta=theta_dollar,
            vega=vega_dollar, iv=sigma,
        )

