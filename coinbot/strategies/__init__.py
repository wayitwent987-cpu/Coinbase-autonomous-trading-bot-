"""Trading strategies and the regime-aware router that selects among them."""

from .base import Strategy, StrategyContext
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .range_scalp import RangeScalpStrategy
from .router import REGISTRY, Decision, StrategyRouter
from .squeeze_break import SqueezeBreakStrategy
from .trend_follow import TrendFollowStrategy

__all__ = [
    "REGISTRY",
    "BreakoutStrategy",
    "Decision",
    "MeanReversionStrategy",
    "RangeScalpStrategy",
    "SqueezeBreakStrategy",
    "Strategy",
    "StrategyContext",
    "StrategyRouter",
    "TrendFollowStrategy",
]
