"""Fitting parameters on real history, and judging how much to believe the fit.

The bot's strategy parameters ship as reasonable guesses. This package turns
them into values chosen on real data — and, more importantly, reports how much
of the resulting improvement is likely to survive contact with next month's
market.

    from coinbot.optimize import SearchSpace, WalkForwardOptimizer

    space = SearchSpace.preset("fast")
    result = WalkForwardOptimizer(config, space, folds=4).run(candles)
    print(result.render())

The order of operations is deliberate: search on training windows only, score
on the untouched window that follows, stitch those out-of-sample windows into
one curve, then discount that curve for how many configurations were tried to
find it.
"""

from .objective import DEFAULT_OBJECTIVE, OBJECTIVES, Objective, get_objective
from .robustness import (
    DeflatedSharpe,
    MonteCarloResult,
    deflated_sharpe,
    monte_carlo,
    returns_from_curve,
)
from .space import PRESETS, Candidate, SearchSpace, SpaceError, inert_dimensions
from .walkforward import (
    Fold,
    FoldResult,
    WalkForwardOptimizer,
    WalkForwardResult,
    make_folds,
)

__all__ = [
    "Candidate",
    "DEFAULT_OBJECTIVE",
    "DeflatedSharpe",
    "Fold",
    "FoldResult",
    "MonteCarloResult",
    "OBJECTIVES",
    "Objective",
    "PRESETS",
    "SearchSpace",
    "SpaceError",
    "WalkForwardOptimizer",
    "WalkForwardResult",
    "deflated_sharpe",
    "get_objective",
    "inert_dimensions",
    "make_folds",
    "monte_carlo",
    "returns_from_curve",
]
