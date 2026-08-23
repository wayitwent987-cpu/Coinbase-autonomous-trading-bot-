"""Adaptive sizing from realized performance.

The bot keeps a scorecard for every (strategy, market regime) pair and scales
the size of future trades by how that specific combination has actually done.
A breakout strategy that works in trends but bleeds in chop gets sized down in
chop without being switched off anywhere else.

Three design decisions matter more than the formula:

* **Shrinkage toward neutral.** A strategy that lost twice is not a bad
  strategy, it is a small sample. The score is pulled toward 1.0 in proportion
  to how little evidence exists, so the bot adapts slowly at first and
  confidently later. Without this, the tracker would chase noise and amplify
  it — the exact failure mode that makes naive "self-learning" bots worse than
  static ones.

* **It never fully disables a combination.** The floor is a small positive
  multiplier, not zero. Markets change; a setup that failed for a month can
  work again, and a combination sized to zero stops producing the data needed
  to notice. Permanent bans are how a bot ends up optimized for a regime that
  no longer exists.

* **It only shrinks and modestly grows.** The ceiling is deliberately close to
  1.0. Sizing *up* after a winning streak is how accounts blow up, because
  winning streaks are the most common thing to find in noise.

What this does: stop repeating a pattern that has demonstrably lost money.
What this does not do: predict the future, or prevent losses.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..models import Regime, TradeRecord
from ..util.numeric import clamp

log = get_logger("adaptive")


@dataclass
class CombinationStats:
    """Realized results for one (strategy, regime) pair."""

    strategy: str
    regime: str
    trades: int = 0
    wins: int = 0
    sum_return: float = 0.0        # sum of net return fractions
    sum_return_sq: float = 0.0     # for the dispersion estimate
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    last_updated: float = 0.0
    recent: List[float] = field(default_factory=list)  # rolling window

    @property
    def key(self) -> Tuple[str, str]:
        return (self.strategy, self.regime)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def mean_return(self) -> float:
        """Average net return per trade, as a fraction of position cost."""
        return self.sum_return / self.trades if self.trades else 0.0

    @property
    def stdev_return(self) -> float:
        if self.trades < 2:
            return 0.0
        mean = self.mean_return
        var = (self.sum_return_sq / self.trades) - (mean * mean)
        return math.sqrt(max(0.0, var))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss <= 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    def record(self, net_return: float, is_win: bool, ts: float, window: int) -> None:
        self.trades += 1
        self.wins += int(is_win)
        self.sum_return += net_return
        self.sum_return_sq += net_return * net_return
        if net_return >= 0:
            self.gross_profit += net_return
        else:
            self.gross_loss += abs(net_return)
        self.last_updated = ts
        self.recent.append(net_return)
        if len(self.recent) > window:
            del self.recent[: len(self.recent) - window]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CombinationStats":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class PerformanceTracker:
    """Scores (strategy, regime) pairs and turns those scores into size scalars."""

    def __init__(
        self,
        *,
        confidence_trades: int = 20,
        min_trades: int = 5,
        reference_return: float = 0.005,
        floor: float = 0.25,
        ceiling: float = 1.25,
        gain: float = 1.0,
        window: int = 50,
        enabled: bool = True,
    ) -> None:
        """
        ``min_trades`` is a hard floor on evidence: below it the tracker stays
        exactly neutral rather than reacting to a handful of results.
        ``confidence_trades`` is the shrinkage constant above that: at this many
        trades the score carries half its full weight. ``reference_return`` is
        the average net return per trade treated as "good" (0.5% by default).
        """
        if confidence_trades < 1:
            raise ValueError("confidence_trades must be >= 1")
        if not 0.0 < floor <= 1.0 <= ceiling:
            raise ValueError("require 0 < floor <= 1 <= ceiling")

        self.confidence_trades = confidence_trades
        self.min_trades = max(0, min_trades)
        self.reference_return = max(1e-9, reference_return)
        self.floor = floor
        self.ceiling = ceiling
        self.gain = gain
        self.window = window
        self.enabled = enabled
        self._stats: Dict[Tuple[str, str], CombinationStats] = {}

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def record_trade(self, trade: TradeRecord) -> None:
        """Fold one completed round trip into the scorecard."""
        strategy = trade.strategy or "unknown"
        regime = trade.regime.value if isinstance(trade.regime, Regime) else str(trade.regime)
        key = (strategy, regime)

        stats = self._stats.get(key)
        if stats is None:
            stats = CombinationStats(strategy=strategy, regime=regime)
            self._stats[key] = stats

        # Net of fees: a strategy whose gross edge is smaller than its costs is
        # a losing strategy, and scoring it gross would hide that.
        net_return = trade.net_return_pct
        stats.record(net_return, trade.net_pnl > 0, trade.closed_at, self.window)

        multiplier = self.multiplier(strategy, regime)
        log.info(
            "adaptive: %s/%s now %d trades, win %.0f%%, mean %.3f%% -> size x%.2f",
            strategy, regime, stats.trades, stats.win_rate * 100,
            stats.mean_return * 100, multiplier,
        )

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #

    def multiplier(self, strategy: str, regime: str) -> float:
        """Size scalar in ``[floor, ceiling]``; 1.0 means "no opinion yet"."""
        if not self.enabled:
            return 1.0
        stats = self._stats.get((strategy, regime))
        if stats is None or stats.trades < max(1, self.min_trades):
            # Not enough evidence to have an opinion. Two losses in a row is a
            # small sample, not a verdict, and reacting to it would just add
            # noise to the position sizing.
            return 1.0

        # Shrink the observed mean toward zero by sample size. With n trades the
        # estimate carries weight n / (n + k), so two trades barely move it and
        # a hundred nearly fully determine it.
        weight = stats.trades / (stats.trades + self.confidence_trades)
        shrunk = stats.mean_return * weight

        # Penalise erratic results: the same average return is worth less when
        # it comes with wild dispersion.
        dispersion = stats.stdev_return
        if dispersion > 0:
            consistency = clamp(
                abs(stats.mean_return) / (dispersion + 1e-12), 0.0, 1.0
            )
            shrunk *= 0.5 + 0.5 * consistency

        raw = 1.0 + self.gain * (shrunk / self.reference_return)
        return clamp(raw, self.floor, self.ceiling)

    def multiplier_for(self, strategy: str, regime: Regime) -> float:
        return self.multiplier(strategy, regime.value)

    def explain(self, strategy: str, regime: str) -> str:
        stats = self._stats.get((strategy, regime))
        if stats is None or stats.trades == 0:
            return f"{strategy}/{regime}: no history, size x1.00"
        return (
            f"{strategy}/{regime}: {stats.trades} trades, "
            f"win {stats.win_rate:.0%}, mean {stats.mean_return:+.2%}, "
            f"pf {stats.profit_factor:.2f} -> size x{self.multiplier(strategy, regime):.2f}"
        )

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #

    def get(self, strategy: str, regime: str) -> Optional[CombinationStats]:
        return self._stats.get((strategy, regime))

    def all_stats(self) -> List[CombinationStats]:
        return sorted(
            self._stats.values(), key=lambda s: s.mean_return * s.trades, reverse=True
        )

    def worst_combinations(self, minimum_trades: int = 10) -> List[CombinationStats]:
        """Combinations with enough evidence to call them losers."""
        return [
            s for s in self.all_stats()
            if s.trades >= minimum_trades and s.mean_return < 0
        ]

    def report(self) -> str:
        if not self._stats:
            return "adaptive: no completed trades yet"
        lines = ["strategy/regime          trades   win    mean     size"]
        lines.append("-" * 56)
        for stats in self.all_stats():
            lines.append(
                f"{stats.strategy + '/' + stats.regime:<24} "
                f"{stats.trades:>6}  {stats.win_rate:>4.0%}  "
                f"{stats.mean_return:>+6.2%}   x{self.multiplier(stats.strategy, stats.regime):.2f}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def to_json(self) -> str:
        return json.dumps([s.to_dict() for s in self._stats.values()])

    def load_json(self, payload: str) -> None:
        """Restore the scorecard. Corrupt entries are skipped, not fatal."""
        if not payload:
            return
        try:
            rows = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            log.warning("adaptive: stored performance data is unreadable; starting fresh")
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                stats = CombinationStats.from_dict(row)
            except (TypeError, ValueError):
                continue
            if stats.strategy and stats.regime:
                self._stats[stats.key] = stats
        if self._stats:
            log.info("adaptive: restored %d strategy/regime scorecards", len(self._stats))

    def reset(self) -> None:
        self._stats.clear()


__all__ = ["CombinationStats", "PerformanceTracker"]
