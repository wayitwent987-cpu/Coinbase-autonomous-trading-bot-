"""Turning a backtest result into one number the optimizer can rank on.

What you optimize is what you get, and the obvious choice is the wrong one.
Ranking on total return picks the configuration that took the biggest bet at
the luckiest moment; ranking on any raw ratio picks the configuration that
took three trades and won all three. Both look excellent on the fold they were
fitted to and neither survives contact with the next one.

Every objective here therefore does two things beyond its headline statistic:

* **Penalizes thin samples.** A result from fewer than ``min_trades`` trades is
  scaled down toward zero rather than trusted. Twelve trades cannot distinguish
  an edge from a coin flip, however good the ratio looks.
* **Penalizes drawdown past a soft threshold.** A configuration that returns
  40% through a 45% drawdown is not better than one returning 25% through 12%.
  The first is unholdable, and the account may not survive to collect it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from ..backtest.metrics import Metrics


@dataclass(frozen=True)
class Objective:
    """A scoring rule. Higher is always better; ``-inf`` means unusable."""

    name: str
    #: Raw statistic pulled from the metrics, before penalties.
    statistic: Callable[[Metrics], float]
    #: Below this trade count the score is scaled toward zero.
    min_trades: int = 30
    #: Drawdown beyond this fraction is penalized.
    drawdown_soft_cap: float = 0.20
    #: How hard the drawdown penalty bites.
    drawdown_weight: float = 2.0
    description: str = ""

    def score(self, metrics: Metrics) -> float:
        """Score ``metrics``; higher is better."""
        # A run that never traded is not a cautious success, it is no evidence
        # at all. Ranking it as 0.0 would let it beat every losing candidate
        # and win the fold by default.
        if metrics.total_trades == 0:
            return float("-inf")

        raw = self.statistic(metrics)
        if raw is None or math.isnan(raw):
            return float("-inf")
        if math.isinf(raw):
            # Profit factor and Sortino go infinite when there are no losses at
            # all, which on a real tape means the sample is too small to have
            # found one yet. Treat it as a large but finite, still-penalizable
            # number instead of an unbeatable score.
            raw = 1e6 if raw > 0 else -1e6

        return raw * self.confidence(metrics) * self.drawdown_multiplier(metrics)

    # ------------------------------------------------------------------ #

    def confidence(self, metrics: Metrics) -> float:
        """Weight in ``(0, 1]`` reflecting how much sample there is.

        Square-root scaling, because the standard error of a mean shrinks with
        the square root of the sample: 30 trades is not three times as
        trustworthy as 10, it is about 1.7 times.
        """
        if self.min_trades <= 0:
            return 1.0
        if metrics.total_trades >= self.min_trades:
            return 1.0
        return math.sqrt(metrics.total_trades / self.min_trades)

    def drawdown_multiplier(self, metrics: Metrics) -> float:
        """Shrink a score whose drawdown ran past the soft cap.

        Applied as a multiplier rather than a subtraction so it behaves the
        same way regardless of the statistic's units — Sharpe and total return
        do not live on the same scale.
        """
        excess = metrics.max_drawdown - self.drawdown_soft_cap
        if excess <= 0:
            return 1.0
        multiplier = 1.0 / (1.0 + self.drawdown_weight * excess)
        # A drawdown at or past 100% means ruin; nothing recommends it.
        if metrics.max_drawdown >= 1.0:
            return 0.0
        return multiplier

    def explain(self, metrics: Metrics) -> Dict[str, float]:
        """The score's components, for reporting why a candidate ranked where it did."""
        raw = self.statistic(metrics)
        if raw is not None and math.isinf(raw):
            raw = 1e6 if raw > 0 else -1e6
        return {
            "raw": float(raw or 0.0),
            "confidence": self.confidence(metrics),
            "drawdown_multiplier": self.drawdown_multiplier(metrics),
            "score": self.score(metrics),
        }


def _risk_adjusted_return(m: Metrics) -> float:
    """Total return divided by the drawdown it was earned through.

    The question a trader actually asks — how much did I make per unit of the
    worst thing I had to sit through — expressed so that it works on samples
    too short to annualize. A tiny drawdown floor keeps a lucky, barely-tested
    configuration from dividing its way to an enormous score.
    """
    return m.total_return / max(m.max_drawdown, 0.02)


#: The built-in objectives.
OBJECTIVES: Dict[str, Objective] = {
    "risk_adjusted": Objective(
        name="risk_adjusted",
        statistic=_risk_adjusted_return,
        description="return per unit of max drawdown (default)",
    ),
    "sharpe": Objective(
        name="sharpe",
        statistic=lambda m: m.sharpe,
        description="return per unit of volatility",
    ),
    "sortino": Objective(
        name="sortino",
        statistic=lambda m: m.sortino,
        description="return per unit of downside volatility",
    ),
    "calmar": Objective(
        name="calmar",
        statistic=lambda m: m.calmar if m.annualized_valid else _risk_adjusted_return(m),
        description="annualized return over max drawdown",
    ),
    "profit_factor": Objective(
        name="profit_factor",
        statistic=lambda m: m.profit_factor,
        description="gross wins over gross losses",
    ),
    "expectancy": Objective(
        name="expectancy",
        statistic=lambda m: m.expectancy,
        description="average profit per trade in quote currency",
    ),
    # Included because people ask for it, and flagged for what it is.
    "total_return": Objective(
        name="total_return",
        statistic=lambda m: m.total_return,
        min_trades=50,
        drawdown_soft_cap=0.15,
        drawdown_weight=4.0,
        description="raw return — the most overfit-prone choice; drawdown penalty is stricter to compensate",
    ),
}

DEFAULT_OBJECTIVE = "risk_adjusted"


def get_objective(name: Optional[str] = None) -> Objective:
    """Look up an objective by name."""
    key = name or DEFAULT_OBJECTIVE
    try:
        return OBJECTIVES[key]
    except KeyError:
        raise ValueError(
            f"unknown objective '{key}'; available: {', '.join(sorted(OBJECTIVES))}"
        ) from None


__all__ = ["DEFAULT_OBJECTIVE", "OBJECTIVES", "Objective", "get_objective"]
