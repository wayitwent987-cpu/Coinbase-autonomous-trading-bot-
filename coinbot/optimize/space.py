"""The set of parameters an optimizer is allowed to move, and how.

A search space is a mapping of dotted config paths to the values each may
take::

    risk.atr_stop_multiple:                 [1.8, 2.2, 2.8]
    strategy.params.trend_follow.adx_min:   [18.0, 22.0, 28.0]

Two rules keep a search honest, and both are enforced here rather than left to
the caller's discipline:

**Every path must resolve.** A typo like ``risk.atr_stop_mult`` would otherwise
be silently ignored, and the optimizer would report that a parameter it never
actually varied is the one that matters. Unknown paths raise.

**Risk limits are not tunable.** Drawdown halts, daily loss limits and leverage
caps exist to bound the worst case; an optimizer pointed at return will always
discover that removing them raises the backtested number. They are rejected
outright, so no search can quietly trade a circuit breaker for a better score.
"""

from __future__ import annotations

import copy
import itertools
import random
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..config import Config, ConfigError

#: Config paths an optimizer may never touch, matched as prefixes.
#:
#: These are the controls that bound the worst case. Every one of them looks
#: like dead weight to a search maximizing return — the run where the breaker
#: never fired scores higher than the run where it saved the account — so the
#: optimizer is not given the option.
FROZEN_PATHS: Tuple[str, ...] = (
    "risk.max_drawdown_pct",
    "risk.daily_loss_limit_pct",
    "risk.weekly_loss_limit_pct",
    "risk.max_consecutive_losses",
    "risk.halt_on_drawdown",
    "risk.max_leverage",
    "risk.hard_stop_pct",
    "risk.min_position_notional",
    "api",
    "storage",
    "logging",
    "trading.dry_run",
    "trading.products",
    "backtest.fee_bps",
    "backtest.slippage_bps",
    "execution.taker_fee_bps",
    "execution.maker_fee_bps",
    "execution.slippage_bps",
)

#: Strategy names whose ``strategy.params.<name>.<key>`` paths are writable.
#: Taken from the config's own defaults so the two cannot drift apart.
_STRATEGY_PARAM_PREFIX = "strategy.params."


class SpaceError(ConfigError):
    """A search space refers to something that does not exist, or must not move."""


@dataclass(frozen=True)
class Candidate:
    """One point in the space: a mapping of dotted path to chosen value."""

    values: Dict[str, Any]

    def key(self) -> Tuple[Tuple[str, str], ...]:
        """A hashable identity, usable for de-duplicating candidates."""
        return tuple(sorted((k, repr(v)) for k, v in self.values.items()))

    def render(self) -> str:
        if not self.values:
            return "(baseline)"
        return ", ".join(f"{k.split('.')[-1]}={_fmt(v)}" for k, v in sorted(self.values.items()))

    def apply(self, config: Config) -> Config:
        """Return a copy of ``config`` with this candidate's values set."""
        out = copy.deepcopy(config)
        for path, value in self.values.items():
            set_path(out, path, value)
        out.validate()
        return out


@dataclass
class SearchSpace:
    """A named set of parameters and the values each may take."""

    dimensions: Dict[str, List[Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        probe = Config()
        for path, values in self.dimensions.items():
            if not values:
                raise SpaceError(f"search space dimension '{path}' has no values")
            check_path(probe, path)

    # ------------------------------------------------------------------ #

    @property
    def size(self) -> int:
        """How many distinct points the full grid contains."""
        total = 1
        for values in self.dimensions.values():
            total *= len(values)
        return total

    def grid(self) -> Iterator[Candidate]:
        """Every combination, in a deterministic order."""
        if not self.dimensions:
            yield Candidate({})
            return
        names = sorted(self.dimensions)
        for combo in itertools.product(*(self.dimensions[n] for n in names)):
            yield Candidate(dict(zip(names, combo)))

    def sample(self, n: int, *, seed: int = 0) -> List[Candidate]:
        """``n`` distinct random points, or the whole grid if it is smaller.

        Random search beats grid search once there are more than a handful of
        dimensions: a grid spends its budget re-testing the same value of an
        irrelevant parameter, while random sampling gives every dimension ``n``
        distinct values for the same cost.
        """
        if n <= 0:
            raise SpaceError("sample size must be positive")
        if not self.dimensions:
            return [Candidate({})]
        if self.size <= n:
            return list(self.grid())

        rng = random.Random(seed)
        names = sorted(self.dimensions)
        seen: set = set()
        out: List[Candidate] = []
        # Bounded: a space larger than n always has points left to find, but
        # a pathological rng streak should not spin forever.
        for _ in range(n * 200):
            if len(out) >= n:
                break
            candidate = Candidate({name: rng.choice(self.dimensions[name]) for name in names})
            key = candidate.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out

    def candidates(self, budget: Optional[int] = None, *, seed: int = 0) -> List[Candidate]:
        """Grid if it fits the budget, otherwise a random sample of that size."""
        if budget is None or self.size <= budget:
            return list(self.grid())
        return self.sample(budget, seed=seed)

    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: Dict[str, Sequence[Any]]) -> "SearchSpace":
        if not isinstance(data, dict):
            raise SpaceError("a search space must be a mapping of path -> list of values")
        dimensions: Dict[str, List[Any]] = {}
        for path, values in data.items():
            if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
                raise SpaceError(f"search space '{path}' must map to a list of values")
            dimensions[str(path)] = list(values)
        return cls(dimensions)

    @classmethod
    def preset(cls, name: str) -> "SearchSpace":
        """One of the built-in spaces; see :data:`PRESETS`."""
        try:
            return cls.from_dict(PRESETS[name])
        except KeyError:
            raise SpaceError(
                f"unknown search space '{name}'; available: {', '.join(sorted(PRESETS))}"
            ) from None


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #


def _split(path: str) -> List[str]:
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise SpaceError("empty config path")
    return parts


def check_path(config: Config, path: str) -> None:
    """Raise unless ``path`` names a real, tunable config field."""
    for frozen in FROZEN_PATHS:
        if path == frozen or path.startswith(frozen + "."):
            raise SpaceError(
                f"'{path}' may not be optimized: it is a risk limit, a credential, "
                f"or a cost assumption. Tuning these makes a backtest look better "
                f"without making the strategy better."
            )

    if path.startswith(_STRATEGY_PARAM_PREFIX):
        _check_strategy_param(config, path)
        return

    target: Any = config
    parts = _split(path)
    for i, part in enumerate(parts):
        if not is_dataclass(target) or not any(f.name == part for f in fields(target)):
            known = (
                ", ".join(sorted(f.name for f in fields(target)))
                if is_dataclass(target)
                else "(not a config section)"
            )
            raise SpaceError(
                f"unknown config path '{path}': no field '{part}' at "
                f"'{'.'.join(parts[:i]) or 'config'}'. Known: {known}"
            )
        target = getattr(target, part)


def _check_strategy_param(config: Config, path: str) -> None:
    """Validate ``strategy.params.<strategy>.<key>``."""
    parts = _split(path)
    if len(parts) != 4:
        raise SpaceError(
            f"'{path}' is not a strategy parameter path; expected "
            f"strategy.params.<strategy>.<parameter>"
        )
    _, _, strategy_name, key = parts

    defaults = _strategy_defaults()
    if strategy_name not in defaults:
        raise SpaceError(
            f"unknown strategy '{strategy_name}' in '{path}'; "
            f"known: {', '.join(sorted(defaults))}"
        )
    if key not in defaults[strategy_name]:
        raise SpaceError(
            f"strategy '{strategy_name}' has no parameter '{key}'; "
            f"known: {', '.join(sorted(defaults[strategy_name]))}"
        )


def _strategy_defaults() -> Dict[str, Dict[str, Any]]:
    """``{strategy_name: default_params}`` for every strategy class."""
    from ..strategies.router import REGISTRY  # local import: avoids a cycle

    out: Dict[str, Dict[str, Any]] = {}
    for name, cls in REGISTRY.items():
        out[name] = dict(getattr(cls, "defaults", {}) or {})
    return out


def set_path(config: Config, path: str, value: Any) -> None:
    """Assign ``value`` at ``path``, creating strategy param dicts as needed."""
    check_path(config, path)
    parts = _split(path)

    if path.startswith(_STRATEGY_PARAM_PREFIX):
        _, _, strategy_name, key = parts
        params = config.strategy.params.setdefault(strategy_name, {})
        params[key] = value
        return

    target: Any = config
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def get_path(config: Config, path: str) -> Any:
    """Read the current value at ``path``, falling back to strategy defaults."""
    parts = _split(path)
    if path.startswith(_STRATEGY_PARAM_PREFIX):
        _, _, strategy_name, key = parts
        configured = config.strategy.params.get(strategy_name, {})
        if key in configured:
            return configured[key]
        return _strategy_defaults().get(strategy_name, {}).get(key)

    target: Any = config
    for part in parts:
        target = getattr(target, part)
    return target


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# --------------------------------------------------------------------------- #
# Built-in spaces
# --------------------------------------------------------------------------- #

#: Ready-made spaces, from cheapest to broadest.
#:
#: Sizes matter: every extra point is another chance to find a configuration
#: that fits noise. ``fast`` is deliberately small enough to run on a laptop,
#: and broad spaces are meant to be paired with a sampling budget rather than
#: run as a full grid.
PRESETS: Dict[str, Dict[str, Sequence[Any]]] = {
    # Exits dominate realized P&L far more than entries do: the same entries
    # with a wider stop and a further target is a different strategy.
    #
    # ``override_strategy_exits`` is pinned on because without it the two
    # multiples below are inert — each strategy's own stop and target win, and
    # the search would spend its whole budget discovering that nothing it
    # changed mattered.
    "exits": {
        "risk.override_strategy_exits": [True],
        "risk.atr_stop_multiple": [1.6, 2.2, 3.0],
        "risk.take_profit_r": [1.5, 2.5, 4.0],
        "risk.atr_trail_multiple": [2.0, 3.0, 4.5],
        "risk.use_trailing_stop": [True, False],
    },
    # The same question asked per strategy, leaving each one's own exit logic
    # in charge. Slower to search than "exits" but it does not force every
    # strategy onto one stop distance.
    "strategy_exits": {
        "strategy.params.trend_follow.stop_atr": [1.8, 2.2, 3.0],
        "strategy.params.trend_follow.target_r": [2.0, 3.0, 4.5],
        "strategy.params.breakout.stop_atr": [1.5, 2.0, 2.8],
        "strategy.params.breakout.target_r": [2.0, 3.0, 4.5],
        "strategy.params.mean_reversion.stop_atr": [1.5, 2.0, 2.8],
    },
    # How selective the bot is about acting at all.
    "entries": {
        "strategy.min_signal_strength": [0.25, 0.35, 0.5],
        "strategy.require_htf_alignment": [True, False],
        "regime.adx_trend_threshold": [18.0, 23.0, 28.0],
        "strategy.params.trend_follow.adx_min": [18.0, 22.0, 28.0],
    },
    # A small, fast space touching one knob from each family. Sized to finish
    # on a laptop, which matters more than breadth for a first look.
    "fast": {
        "risk.override_strategy_exits": [True],
        "risk.atr_stop_multiple": [1.8, 2.2, 2.8],
        "risk.take_profit_r": [2.0, 3.0],
        "strategy.min_signal_strength": [0.3, 0.45],
    },
    # Position sizing and concurrency. Note that risk_per_trade_pct is bounded
    # well below the config's own 10% ceiling: a search told to maximize return
    # takes the largest size on offer every time, and the largest size on offer
    # is how accounts end.
    "sizing": {
        "risk.risk_per_trade_pct": [0.005, 0.0075, 0.01, 0.015],
        "risk.max_position_pct": [0.15, 0.25, 0.35],
        "risk.max_open_positions": [2, 3, 4],
        "risk.use_vol_targeting": [True, False],
    },
    # Everything that matters, and far too large for a grid — pair with
    # --budget so it is sampled rather than enumerated.
    "broad": {
        "risk.override_strategy_exits": [True],
        "risk.atr_stop_multiple": [1.6, 2.2, 2.8, 3.4],
        "risk.take_profit_r": [1.5, 2.5, 3.5, 5.0],
        "risk.atr_trail_multiple": [2.0, 3.0, 4.5],
        "risk.use_trailing_stop": [True, False],
        "risk.breakeven_at_r": [0.5, 1.0, 1.5],
        "risk.risk_per_trade_pct": [0.005, 0.0075, 0.01],
        "risk.max_position_pct": [0.15, 0.25, 0.35],
        "risk.max_bars_in_trade": [96, 288, 576],
        "strategy.min_signal_strength": [0.25, 0.35, 0.5],
        "strategy.require_htf_alignment": [True, False],
        "regime.adx_trend_threshold": [18.0, 23.0, 28.0],
        "regime.hysteresis_bars": [1, 2, 4],
    },
}


#: Paths that only do anything when ``risk.override_strategy_exits`` is on.
#:
#: Every strategy supplies its own stop and target, so by default these two
#: are fallbacks that never fire. Searching them without the override produces
#: a flat objective and a meaningless "winner".
REQUIRES_EXIT_OVERRIDE: Tuple[str, ...] = (
    "risk.atr_stop_multiple",
    "risk.take_profit_r",
)


def inert_dimensions(space: "SearchSpace", config: Config) -> List[str]:
    """Dimensions in ``space`` that cannot affect a run under ``config``."""
    varies_override = "risk.override_strategy_exits" in space.dimensions
    if varies_override and any(space.dimensions["risk.override_strategy_exits"]):
        return []
    if config.risk.override_strategy_exits:
        return []
    return [p for p in REQUIRES_EXIT_OVERRIDE if p in space.dimensions]


__all__ = [
    "Candidate",
    "FROZEN_PATHS",
    "PRESETS",
    "REQUIRES_EXIT_OVERRIDE",
    "SearchSpace",
    "SpaceError",
    "check_path",
    "get_path",
    "inert_dimensions",
    "set_path",
]
