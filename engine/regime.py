"""
Regime Detector — realized-volatility market-regime classification for sizing.

WHY (Session 9): the bot sized identically in a calm tape and a crashing one —
conviction scoring knew nothing about market conditions, and the only defence
was the daily-loss circuit breaker, which fires AFTER the damage.  This module
is the forward-looking cousin: classify the tape BEFORE entering, and let the
decision engine scale percentage-tier sizing down (1.0 / 0.6 / 0.3 by default)
when volatility is elevated or stressed.

Inspired by the regime-switching HMM literature (e.g. 3-state Hidden Markov
Models for bull/bear/neutral factor rotation), but deliberately much simpler:
annualized realized volatility of recent daily closes against two fixed
thresholds.  An HMM is a possible future refinement once the simple version
proves out — for a $1k account mirroring fast option callers, three
realized-vol buckets capture most of the benefit at none of the complexity.

Usage (wired in main.py):
    detector = RegimeDetector(config, executor)
    state = detector.refresh()          # {"label", "multiplier", "annualized_vol"}
    engine.set_regime(state["multiplier"], state["label"])

Config block (config.yaml, read with defaults so a missing block is safe):
    regime:
      enabled: true
      symbol: "SPY"          # data only — never traded (SPY is UK-blocked for orders)
      lookback_days: 6       # daily closes fetched (5 log-returns)
      calm_vol: 0.15         # annualized vol below this → "calm"
      stressed_vol: 0.28     # annualized vol above this → "stressed"
      multipliers:
        calm: 1.0
        elevated: 0.6
        stressed: 0.3
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MULTIPLIERS = {"calm": 1.0, "elevated": 0.6, "stressed": 0.3}


class RegimeDetector:
    """
    Classifies the market regime from recent daily closes of a reference
    symbol and maps it to a sizing multiplier.  Never raises — any failure
    degrades to ("unknown", multiplier 1.0), i.e. normal sizing.
    """

    def __init__(self, config: dict, executor=None):
        rc = (config or {}).get("regime", {}) or {}
        self.enabled: bool = rc.get("enabled", True)
        self.symbol: str = rc.get("symbol", "SPY")
        self.lookback_days: int = int(rc.get("lookback_days", 6))
        self.calm_vol: float = float(rc.get("calm_vol", 0.15))
        self.stressed_vol: float = float(rc.get("stressed_vol", 0.28))
        self.multipliers: dict = {**_DEFAULT_MULTIPLIERS, **(rc.get("multipliers") or {})}
        self.executor = executor
        # Last refresh() result, for status displays / logging
        self.last: dict = {"label": "unknown", "multiplier": 1.0, "annualized_vol": None}

    @staticmethod
    def classify_closes(
        closes: list, calm_vol: float = 0.15, stressed_vol: float = 0.28
    ) -> tuple[str, Optional[float]]:
        """
        Classify a series of daily closes into a regime label.

        Returns (label, annualized_vol):
          - annualized vol = population std of daily log-returns * sqrt(252)
          - vol < calm_vol      → "calm"
          - vol > stressed_vol  → "stressed"
          - otherwise           → "elevated"
          - fewer than 4 closes (or unusable data) → ("unknown", None)
        """
        if not closes or len(closes) < 4:
            return "unknown", None

        try:
            values = [float(c) for c in closes]
        except (TypeError, ValueError):
            return "unknown", None
        if any(v <= 0 for v in values):
            return "unknown", None

        returns = [
            math.log(values[i] / values[i - 1]) for i in range(1, len(values))
        ]
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / n  # population
        annualized_vol = math.sqrt(variance) * math.sqrt(252)

        if annualized_vol < calm_vol:
            label = "calm"
        elif annualized_vol > stressed_vol:
            label = "stressed"
        else:
            label = "elevated"
        return label, annualized_vol

    def refresh(self) -> dict:
        """
        Re-classify the current regime.  Returns and caches
        {"label": str, "multiplier": float, "annualized_vol": float | None}.
        Never raises — failures degrade to normal (1.0) sizing.
        """
        try:
            if not self.enabled or self.executor is None:
                self.last = {
                    "label": "disabled",
                    "multiplier": 1.0,
                    "annualized_vol": None,
                }
                return self.last

            closes = self.executor.get_daily_closes(self.symbol, self.lookback_days) or []
            label, vol = self.classify_closes(closes, self.calm_vol, self.stressed_vol)
            # "unknown" (and anything not in the multipliers map) → 1.0
            multiplier = float(self.multipliers.get(label, 1.0))

            self.last = {
                "label": label,
                "multiplier": multiplier,
                "annualized_vol": vol,
            }
            logger.info(
                f"Regime refresh: {label} "
                f"(annualized vol: {f'{vol:.1%}' if vol is not None else 'n/a'}, "
                f"{self.symbol} {len(closes)} closes, sizing x{multiplier:g})"
            )
        except Exception as e:
            logger.error(f"Regime refresh failed ({e}) — defaulting to normal sizing")
            self.last = {"label": "unknown", "multiplier": 1.0, "annualized_vol": None}
        return self.last
