"""How much of a good result was skill, and how much was the order of the deck.

A walk-forward result is a single sample: one sequence of trades, in one
order, on one stretch of history. It answers "what happened" but not "how
easily could this have gone differently", and the second question is the one
that decides how much capital it is safe to put behind the answer.

Two checks here, doing different jobs:

**Monte Carlo resampling** shuffles and resamples the realized trades to build
the distribution the single observed path was drawn from. The equity total is
barely affected by ordering; the *drawdown* is affected enormously. Six losses
scattered through a year is a rough patch, and the same six consecutively is a
28% hole and a breached risk limit. The observed drawdown is one draw from that
distribution, and usually a mild one, so sizing to it is how a system that
"never drew down more than 12%" produces a 25% hole in month two.

**Deflated Sharpe** answers the multiple-testing question. Try 400
configurations on random data and the best one will look good — that is what
"best of 400" means, not evidence of anything. This adjusts the observed Sharpe
for how many were tried, and reports the probability that the true Sharpe is
above zero at all.

Neither check can rescue a strategy that does not work. Both can tell you when
one that looks like it works has not yet proved it.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..models import TradeRecord

# The Euler-Mascheroni constant, needed for the expected maximum of a set of
# draws from a normal distribution in the deflated-Sharpe calculation.
_EULER = 0.5772156649015329


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #


@dataclass
class MonteCarloResult:
    """The distribution of outcomes implied by the realized trades."""

    simulations: int = 0
    trades_per_run: int = 0
    observed_return: float = 0.0
    observed_max_drawdown: float = 0.0

    return_percentiles: Dict[int, float] = field(default_factory=dict)
    drawdown_percentiles: Dict[int, float] = field(default_factory=dict)
    #: Share of simulated paths that ended below the starting stake.
    probability_of_loss: float = 0.0
    #: Share that at some point lost more than ``ruin_threshold``.
    probability_of_ruin: float = 0.0
    ruin_threshold: float = 0.5
    median_return: float = 0.0
    median_max_drawdown: float = 0.0

    def render(self) -> str:
        lines = [
            "=" * 74,
            f"MONTE CARLO  ({self.simulations:,} resampled paths of "
            f"{self.trades_per_run} trades)",
            "=" * 74,
            "  Resampling the same trades in different orders. The observed path",
            "  is one draw from this distribution, not the typical case.",
            "",
            f"    {'percentile':<14}{'return':>14}{'max drawdown':>18}",
        ]
        for pct in sorted(self.return_percentiles):
            lines.append(
                f"    {str(pct) + 'th':<14}{self.return_percentiles[pct]:>14.2%}"
                f"{self.drawdown_percentiles.get(pct, 0.0):>18.2%}"
            )
        lines += [
            "",
            f"    {'Observed':<14}{self.observed_return:>14.2%}"
            f"{self.observed_max_drawdown:>18.2%}",
            "-" * 74,
            f"  Chance of ending down            {self.probability_of_loss:>7.1%}",
            f"  Chance of losing over {self.ruin_threshold:.0%}       "
            f"{self.probability_of_ruin:>9.1%}",
        ]
        worst = self.drawdown_percentiles.get(95)
        if worst and worst > self.observed_max_drawdown * 1.5:
            lines += [
                "",
                f"  The 95th-percentile drawdown ({worst:.1%}) is far worse than the",
                f"  {self.observed_max_drawdown:.1%} actually seen. Size for that number, not this one.",
            ]
        lines.append("=" * 74)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return {
            "simulations": self.simulations,
            "trades_per_run": self.trades_per_run,
            "observed_return": self.observed_return,
            "observed_max_drawdown": self.observed_max_drawdown,
            "return_percentiles": dict(self.return_percentiles),
            "drawdown_percentiles": dict(self.drawdown_percentiles),
            "probability_of_loss": self.probability_of_loss,
            "probability_of_ruin": self.probability_of_ruin,
            "median_return": self.median_return,
            "median_max_drawdown": self.median_max_drawdown,
        }


def monte_carlo(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: float,
    simulations: int = 2000,
    seed: int = 0,
    with_replacement: bool = True,
    ruin_threshold: float = 0.5,
    percentiles: Sequence[int] = (5, 25, 50, 75, 95),
) -> MonteCarloResult:
    """Resample ``trades`` into many alternative equity paths.

    Each trade's P&L is converted to a *return on the equity at the time*, so
    resampling compounds correctly rather than replaying absolute dollar
    amounts against an account of a different size.

    ``with_replacement`` bootstraps — sampling the same number of trades, some
    repeated, some omitted. That treats the realized trades as a sample from a
    process rather than the whole of it, which is the more honest assumption
    and gives wider, more useful tails. Turn it off to permute the exact
    observed set instead, isolating the effect of ordering alone.
    """
    if starting_equity <= 0:
        raise ValueError("starting_equity must be positive")
    result = MonteCarloResult(
        simulations=0,
        trades_per_run=len(trades),
        ruin_threshold=ruin_threshold,
    )
    if not trades or simulations < 1:
        return result

    # Per-trade returns, taken against a running equity so that a 100-dollar
    # win early and the same win late are not treated as identical events.
    returns: List[float] = []
    equity = starting_equity
    for trade in trades:
        net = float(trade.net_pnl)
        returns.append(net / equity if equity > 0 else 0.0)
        equity += net

    observed = _path(returns, ruin_threshold)
    result.observed_return = observed[0]
    result.observed_max_drawdown = observed[1]

    rng = random.Random(seed)
    n = len(returns)
    final_returns: List[float] = []
    drawdowns: List[float] = []
    losses = 0
    ruins = 0

    for _ in range(simulations):
        if with_replacement:
            sample = [returns[rng.randrange(n)] for _ in range(n)]
        else:
            sample = returns[:]
            rng.shuffle(sample)
        total, drawdown, ruined = _path(sample, ruin_threshold)
        final_returns.append(total)
        drawdowns.append(drawdown)
        if total < 0:
            losses += 1
        if ruined:
            ruins += 1

    result.simulations = simulations
    result.return_percentiles = _percentiles(final_returns, percentiles)
    result.drawdown_percentiles = _percentiles(drawdowns, percentiles)
    result.probability_of_loss = losses / simulations
    result.probability_of_ruin = ruins / simulations
    result.median_return = statistics.median(final_returns)
    result.median_max_drawdown = statistics.median(drawdowns)
    return result


def _path(returns: Sequence[float], ruin_threshold: float) -> tuple:
    """Compound ``returns`` into (total return, max drawdown, hit ruin?)."""
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    ruined = False
    for r in returns:
        equity *= 1.0 + r
        # A path that reaches zero is over; it cannot trade its way back.
        if equity <= 0:
            return -1.0, 1.0, True
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
        if 1.0 - equity >= ruin_threshold:
            ruined = True
    return equity - 1.0, max_drawdown, ruined


def _percentiles(values: Sequence[float], wanted: Sequence[int]) -> Dict[int, float]:
    ordered = sorted(values)
    out: Dict[int, float] = {}
    for pct in wanted:
        if not ordered:
            out[pct] = 0.0
            continue
        index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
        out[pct] = ordered[index]
    return out


# --------------------------------------------------------------------------- #
# Multiple-testing adjustment
# --------------------------------------------------------------------------- #


@dataclass
class DeflatedSharpe:
    """An observed Sharpe, discounted for how many configurations were tried."""

    observed_sharpe: float = 0.0
    trials: int = 1
    observations: int = 0
    #: The Sharpe a *worthless* strategy would be expected to post as the best
    #: of ``trials`` attempts, purely from selection.
    expected_max_sharpe: float = 0.0
    #: Spread of Sharpe ratios across the configurations tried. The selection
    #: threshold scales with this: if every configuration scored about the
    #: same, picking the best of them gained little.
    trial_sharpe_stdev: float = 0.0
    #: Probability the true Sharpe exceeds that selection threshold.
    probability: float = 0.0
    skew: float = 0.0
    kurtosis: float = 3.0

    @property
    def passes(self) -> bool:
        """Conventional 95% bar. Not a promise — a minimum."""
        return self.probability >= 0.95

    def render(self) -> str:
        verdict = (
            "clears the selection threshold"
            if self.passes
            else "does NOT clear the selection threshold"
        )
        return "\n".join([
            "=" * 74,
            "DEFLATED SHARPE (adjusting for how many configurations were tried)",
            "=" * 74,
            f"  Observed Sharpe (per bar)     {self.observed_sharpe:>10.4f}",
            f"  Configurations tried          {self.trials:>10d}",
            f"  Return observations           {self.observations:>10d}",
            f"  Spread across configs         {self.trial_sharpe_stdev:>10.4f}",
            f"  Sharpe expected from luck     {self.expected_max_sharpe:>10.4f}",
            f"  P(true Sharpe > 0)            {self.probability:>10.1%}",
            "",
            f"  The best of {self.trials} configurations would be expected to show a",
            f"  Sharpe near {self.expected_max_sharpe:.4f} even with no edge whatsoever.",
            f"  This result {verdict}.",
            "=" * 74,
        ])

    def to_dict(self) -> Dict[str, object]:
        return {
            "observed_sharpe": self.observed_sharpe,
            "trials": self.trials,
            "observations": self.observations,
            "expected_max_sharpe": self.expected_max_sharpe,
            "trial_sharpe_stdev": self.trial_sharpe_stdev,
            "probability": self.probability,
            "passes": self.passes,
        }


def deflated_sharpe(
    returns: Sequence[float],
    *,
    trials: int,
    trial_sharpes: Optional[Sequence[float]] = None,
    observed_sharpe: Optional[float] = None,
) -> DeflatedSharpe:
    """Bailey & López de Prado's deflated Sharpe ratio.

    ``returns`` are per-bar returns of the strategy being judged, and ``trials``
    is how many configurations the search evaluated to arrive at it. Both the
    non-normality of the returns and the number of trials push the bar upward:
    fat tails make a high Sharpe easier to post by chance, and so does trying
    more things.

    ``trial_sharpes`` are the Sharpe ratios of all the configurations tried.
    The selection threshold scales with how much they varied — picking the best
    of 500 near-identical configurations gains almost nothing, while picking
    the best of 500 wildly different ones gains a lot. Without them the
    threshold falls back to the standard error of a single Sharpe estimate,
    ``1/sqrt(n-1)``, which is the conservative assumption.

    The Sharpe here is per-observation, deliberately not annualized —
    annualizing scales the estimate and its error together, so the probability
    is unchanged and the intermediate numbers are easier to check. Pass
    ``trial_sharpes`` on the same per-observation basis.
    """
    result = DeflatedSharpe(trials=max(1, trials), observations=len(returns))
    n = len(returns)
    if n < 3:
        return result

    mean = statistics.fmean(returns)
    stdev = statistics.pstdev(returns)
    if stdev <= 0:
        return result

    sharpe = observed_sharpe if observed_sharpe is not None else mean / stdev
    result.observed_sharpe = sharpe

    # Third and fourth standardized moments: fat tails and asymmetry both
    # change how surprising a given Sharpe is.
    skew = statistics.fmean([((r - mean) / stdev) ** 3 for r in returns])
    kurtosis = statistics.fmean([((r - mean) / stdev) ** 4 for r in returns])
    result.skew = skew
    result.kurtosis = kurtosis

    # How much the tried configurations actually differed in Sharpe. This is
    # the unit the selection threshold is measured in; without it the threshold
    # would be expressed in standard normals and compared against a
    # per-observation Sharpe roughly fifty times smaller.
    if trial_sharpes is not None and len(trial_sharpes) > 1:
        finite = [s for s in trial_sharpes if math.isfinite(s)]
        spread = statistics.pstdev(finite) if len(finite) > 1 else 0.0
    else:
        spread = 0.0
    if spread <= 0:
        spread = 1.0 / math.sqrt(n - 1)
    result.trial_sharpe_stdev = spread

    # Expected maximum of `trials` independent draws from a standard normal,
    # scaled into Sharpe units.
    trials_f = float(result.trials)
    if trials_f <= 1:
        expected_max = 0.0
    else:
        expected_max = spread * (
            (1.0 - _EULER) * _z(1.0 - 1.0 / trials_f)
            + _EULER * _z(1.0 - 1.0 / (trials_f * math.e))
        )
    result.expected_max_sharpe = expected_max

    # Standard error of the Sharpe estimate, corrected for skew and kurtosis.
    variance = (
        1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    ) / (n - 1)
    if variance <= 0:
        return result

    result.probability = _normal_cdf((sharpe - expected_max) / math.sqrt(variance))
    return result


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _z(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to about 1e-9, which is far beyond what this is used for, and it
    avoids taking a dependency on scipy for one function.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)

    low, high = 0.02425, 1.0 - 0.02425
    if p < low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def returns_from_curve(curve: Sequence[tuple]) -> List[float]:
    """Per-bar returns from an equity curve of ``(timestamp, equity)`` points."""
    values = [float(v) for _, v in curve]
    return [
        (cur - prev) / prev
        for prev, cur in zip(values, values[1:])
        if prev > 0
    ]


__all__ = [
    "DeflatedSharpe",
    "MonteCarloResult",
    "deflated_sharpe",
    "monte_carlo",
    "returns_from_curve",
]
