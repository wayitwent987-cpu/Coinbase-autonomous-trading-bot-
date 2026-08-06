"""Regime -> strategy routing, and combining several opinions into one.

This is where "handles multiple market conditions" becomes concrete. Each bar:

1. The regime decides *which* strategies are allowed to speak and how loudly.
2. Every allowed strategy is polled for its opinion.
3. The opinions are combined into a single decision per product.

Disagreement is information. When two strategies want opposite directions the
combiner nets them out, and a near-tie produces a weak signal that the risk
manager will size down or reject outright — which is the correct response to
an unclear market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Type

from ..config import StrategyConfig
from ..logging_setup import get_logger
from ..models import Regime, Signal, SignalAction
from ..util.numeric import clamp, to_decimal
from .base import Strategy, StrategyContext
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .range_scalp import RangeScalpStrategy
from .squeeze_break import SqueezeBreakStrategy
from .trend_follow import TrendFollowStrategy

log = get_logger("router")

#: Every strategy the bot knows how to build, keyed by config name.
REGISTRY: Dict[str, Type[Strategy]] = {
    TrendFollowStrategy.name: TrendFollowStrategy,
    BreakoutStrategy.name: BreakoutStrategy,
    MeanReversionStrategy.name: MeanReversionStrategy,
    RangeScalpStrategy.name: RangeScalpStrategy,
    SqueezeBreakStrategy.name: SqueezeBreakStrategy,
}


@dataclass
class Decision:
    """The router's combined verdict for one product on one bar."""

    product_id: str
    action: SignalAction

    #: Combined conviction in [0, 1]. Gates whether to trade at all.
    strength: float = 0.0

    #: Context-based size scaler in [0, 1] — regime confidence and how much of
    #: the regime's routed weight agreed. Kept separate from ``strength`` so
    #: uncertainty shrinks the position rather than silently blocking entries.
    size_multiplier: float = 1.0

    regime: Regime = Regime.UNKNOWN
    regime_confidence: float = 0.0
    strategy: str = ""
    reason: str = ""
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    contributors: List[Signal] = field(default_factory=list)
    conflict: bool = False

    @property
    def sized_conviction(self) -> float:
        """What the risk manager scales its risk budget by."""
        return clamp(self.strength * self.size_multiplier, 0.0, 1.0)

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def is_exit(self) -> bool:
        return self.action is SignalAction.EXIT

    @property
    def direction(self) -> int:
        if self.action is SignalAction.ENTER_LONG:
            return 1
        if self.action is SignalAction.ENTER_SHORT:
            return -1
        return 0

    def __str__(self) -> str:  # pragma: no cover - display only
        return (
            f"{self.product_id} {self.action.value} strength={self.strength:.2f} "
            f"regime={self.regime.value} via={self.strategy or '-'} :: {self.reason}"
        )


class StrategyRouter:
    """Owns the strategy instances and turns their opinions into a Decision."""

    def __init__(self, cfg: Optional[StrategyConfig] = None) -> None:
        self.cfg = cfg or StrategyConfig()
        self.strategies: Dict[str, Strategy] = {}
        for name in self.cfg.enabled:
            cls = REGISTRY.get(name)
            if cls is None:
                raise ValueError(
                    f"unknown strategy '{name}'; known: {sorted(REGISTRY)}"
                )
            self.strategies[name] = cls(self.cfg.params.get(name))
        log.info("router loaded %d strategies: %s", len(self.strategies), ", ".join(self.strategies))

    # ------------------------------------------------------------------ #

    def active_for(self, regime: Regime) -> Dict[str, float]:
        """Strategy weights for a regime, restricted to loaded strategies."""
        weights = self.cfg.weights_for(regime.value)
        return {name: w for name, w in weights.items() if name in self.strategies}

    def evaluate(self, ctx: StrategyContext) -> Decision:
        """Poll the regime's strategies and combine their signals."""
        # Every strategy sees every bar, even ones it will not trade, so that
        # multi-bar setups stay in sync across regime changes.
        for name, strategy in self.strategies.items():
            try:
                strategy.observe(ctx)
            except Exception:
                log.exception("strategy %s failed observing %s", name, ctx.product_id)

        regime = ctx.regime.regime
        weights = self.active_for(regime)

        # No strategy is willing to trade this regime (CRASH, UNKNOWN). Any
        # open position still needs managing, so exits are collected from the
        # strategy that opened it.
        if not weights:
            if ctx.in_position:
                return self._forced_exit_check(ctx, "no strategy active for regime")
            return self._flat(ctx, f"no strategy routed for {regime.value}")

        signals: List[Signal] = []
        for name, weight in weights.items():
            try:
                signal = self.strategies[name].generate(ctx)
            except Exception:  # a broken strategy must not stop the others
                log.exception("strategy %s raised on %s; skipping", name, ctx.product_id)
                continue
            signal.metadata.setdefault("weight", weight)
            signals.append(signal)

        return self._combine(ctx, signals, weights)

    # ------------------------------------------------------------------ #

    def _combine(
        self,
        ctx: StrategyContext,
        signals: Sequence[Signal],
        weights: Dict[str, float],
    ) -> Decision:
        exits = [s for s in signals if s.action is SignalAction.EXIT]
        longs = [s for s in signals if s.action is SignalAction.ENTER_LONG]
        shorts = [s for s in signals if s.action is SignalAction.ENTER_SHORT]

        # Exits win over entries, always. Being wrong about getting out costs a
        # missed opportunity; being wrong about staying in costs money.
        if exits and ctx.in_position:
            best = max(exits, key=lambda s: s.strength)
            return Decision(
                product_id=ctx.product_id,
                action=SignalAction.EXIT,
                strength=best.strength,
                regime=ctx.regime.regime,
                regime_confidence=ctx.regime.confidence,
                strategy=best.strategy,
                reason=best.reason,
                contributors=list(exits),
            )

        long_score = sum(weights.get(s.strategy, 1.0) * s.strength for s in longs)
        short_score = sum(weights.get(s.strategy, 1.0) * s.strength for s in shorts)

        if long_score <= 0 and short_score <= 0:
            return self._flat(ctx, "no strategy wants a position")

        conflict = bool(longs and shorts)
        if long_score >= short_score:
            winners, action, net = longs, SignalAction.ENTER_LONG, long_score - short_score
        else:
            winners, action, net = shorts, SignalAction.ENTER_SHORT, short_score - long_score

        # Normalize by the weight that actually *voted*, not by every strategy
        # routed to this regime. Silence is abstention, not opposition — and
        # these strategies are mutually exclusive by design (a trend pullback
        # and a breakout cannot both trigger on one bar), so dividing by the
        # full routed weight would make the threshold unreachable.
        participating = sum(
            weights.get(s.strategy, 1.0) for s in list(longs) + list(shorts)
        ) or 1.0
        strength = clamp(net / participating, 0.0, 1.0)

        # Whether to trade is the strategies' call; how *large* to trade is a
        # separate question. Folding regime confidence into the entry test
        # would push every signal under the floor and the bot would simply
        # never trade, so conviction gates entry and context scales size.
        if strength < self.cfg.min_signal_strength:
            return self._flat(
                ctx,
                f"combined strength {strength:.2f} below floor {self.cfg.min_signal_strength:.2f}",
            )

        # Breadth: a signal backed by most of the regime's routed weight is
        # worth more than the same conviction from one lone strategy.
        total_weight = sum(weights.values()) or 1.0
        agreement = clamp(
            sum(weights.get(s.strategy, 1.0) for s in winners) / total_weight, 0.0, 1.0
        )
        size_multiplier = (0.80 + 0.20 * agreement) * clamp(
            0.55 + 0.45 * ctx.regime.confidence, 0.0, 1.0
        )
        if conflict:
            size_multiplier *= 0.7  # strategies disagree: take a smaller bite

        if self.cfg.require_htf_alignment:
            direction = 1 if action is SignalAction.ENTER_LONG else -1
            if ctx.htf_bias != 0 and ctx.htf_bias != direction:
                return self._flat(ctx, "blocked: higher timeframe disagrees")

        lead = max(winners, key=lambda s: weights.get(s.strategy, 1.0) * s.strength)
        stop = self._blend_levels(winners, weights, "stop_price")
        target = self._blend_levels(winners, weights, "take_profit")

        return Decision(
            product_id=ctx.product_id,
            action=action,
            strength=strength,
            size_multiplier=clamp(size_multiplier, 0.0, 1.0),
            regime=ctx.regime.regime,
            regime_confidence=ctx.regime.confidence,
            strategy=lead.strategy,
            reason=self._describe(winners, conflict),
            stop_price=stop if stop is not None else lead.stop_price,
            take_profit=target if target is not None else lead.take_profit,
            contributors=list(signals),
            conflict=conflict,
        )

    def _blend_levels(
        self,
        signals: Sequence[Signal],
        weights: Dict[str, float],
        attr: str,
    ) -> Optional[Decimal]:
        """Weighted average of the agreeing strategies' stop/target levels."""
        total = Decimal(0)
        weight_sum = Decimal(0)
        for signal in signals:
            level = getattr(signal, attr)
            if level is None:
                continue
            weight = to_decimal(max(1e-6, weights.get(signal.strategy, 1.0) * signal.strength))
            total += level * weight
            weight_sum += weight
        if weight_sum == 0:
            return None
        return total / weight_sum

    def _describe(self, winners: Sequence[Signal], conflict: bool) -> str:
        parts = [f"{s.strategy}({s.strength:.2f}): {s.reason}" for s in winners if s.reason]
        text = " | ".join(parts) if parts else "combined signal"
        return f"{text} [contested]" if conflict else text

    def _forced_exit_check(self, ctx: StrategyContext, why: str) -> Decision:
        """Ask the position's own strategy whether to exit, even off-regime."""
        owner = ctx.position.strategy if ctx.position else ""
        strategy = self.strategies.get(owner)
        if strategy is not None:
            try:
                signal = strategy.evaluate_exit(ctx)
            except Exception:
                log.exception("exit check failed for %s on %s", owner, ctx.product_id)
                signal = None
            if signal is not None:
                return Decision(
                    product_id=ctx.product_id,
                    action=SignalAction.EXIT,
                    strength=signal.strength,
                    regime=ctx.regime.regime,
                    regime_confidence=ctx.regime.confidence,
                    strategy=owner,
                    reason=signal.reason,
                    contributors=[signal],
                )
        return self._flat(ctx, why)

    def _flat(self, ctx: StrategyContext, reason: str) -> Decision:
        return Decision(
            product_id=ctx.product_id,
            action=SignalAction.HOLD,
            strength=0.0,
            regime=ctx.regime.regime,
            regime_confidence=ctx.regime.confidence,
            reason=reason,
        )

    def reset(self) -> None:
        """Clear per-strategy state (used between backtest runs)."""
        for strategy in self.strategies.values():
            if hasattr(strategy, "_reset"):
                for pid in list(getattr(strategy, "_squeeze_bars", {})):
                    strategy._reset(pid)  # type: ignore[attr-defined]


__all__ = ["Decision", "REGISTRY", "StrategyRouter"]
