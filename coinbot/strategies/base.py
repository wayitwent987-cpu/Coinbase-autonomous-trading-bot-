"""Strategy interface and the context every strategy is handed.

A strategy's only job is to answer "given this bar, do I want to be long,
short, or out, and how strongly?". It never sees the account, never sizes a
position, and never places an order — the risk manager and execution engine
own those. Keeping strategies opinion-only is what lets the same code run
unchanged in the backtester and live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, FrozenSet, Optional

from ..data.features import FeatureSet
from ..models import Position, Regime, RegimeState, Signal, SignalAction
from ..util.numeric import clamp, to_decimal


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy is allowed to look at."""

    product_id: str
    features: FeatureSet
    regime: RegimeState
    htf: Optional[FeatureSet] = None
    position: Optional[Position] = None
    allow_shorts: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def f(self) -> FeatureSet:
        """Short alias — strategy math reads better as ``ctx.f.rsi``."""
        return self.features

    @property
    def price(self) -> float:
        return self.features.close

    @property
    def in_position(self) -> bool:
        return self.position is not None and self.position.is_open

    @property
    def is_long(self) -> bool:
        return self.in_position and self.position.is_long  # type: ignore[union-attr]

    @property
    def is_short(self) -> bool:
        return self.in_position and self.position.is_short  # type: ignore[union-attr]

    @property
    def htf_bias(self) -> int:
        """+1 / -1 / 0 — the higher timeframe's directional lean."""
        if self.htf is None or not self.htf.is_complete:
            return 0
        if self.htf.ema_fast is None or self.htf.ema_slow is None:
            return 0
        if self.htf.ema_fast > self.htf.ema_slow and self.htf.close > self.htf.ema_fast:
            return 1
        if self.htf.ema_fast < self.htf.ema_slow and self.htf.close < self.htf.ema_fast:
            return -1
        return 0


class Strategy(ABC):
    """Base class for all strategies.

    Subclasses declare which regimes they are willing to trade and implement
    :meth:`evaluate`. The base class handles the guard rails that every
    strategy would otherwise repeat: warm-up checks, regime gating, and the
    short-selling permission check.
    """

    #: Unique key, matching the config's ``strategy.enabled`` entries.
    name: str = "base"

    #: Regimes this strategy is willing to trade at all.
    suitable_regimes: FrozenSet[Regime] = frozenset()

    #: Bars of history required before this strategy will emit an entry.
    min_bars: int = 60

    #: Default parameters, overridable per-strategy from config.
    defaults: Dict[str, Any] = {}

    def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
        self.params: Dict[str, Any] = dict(self.defaults)
        if params:
            unknown = set(params) - set(self.defaults)
            if unknown:
                raise ValueError(
                    f"{self.name}: unknown parameters {sorted(unknown)}; "
                    f"known: {sorted(self.defaults)}"
                )
            self.params.update(params)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def generate(self, ctx: StrategyContext) -> Signal:
        """Guarded wrapper around :meth:`evaluate`."""
        if not ctx.features.is_complete or ctx.features.n_bars < self.min_bars:
            return Signal.hold(ctx.product_id, self.name, "warming up")

        # An open position always gets an exit check, even in a regime this
        # strategy would not newly enter — otherwise a regime flip would strand
        # the position with nobody willing to manage it.
        if ctx.in_position:
            exit_signal = self.evaluate_exit(ctx)
            if exit_signal is not None:
                return exit_signal

        if not self.accepts_regime(ctx.regime.regime):
            return Signal.hold(ctx.product_id, self.name, f"regime {ctx.regime.regime.value} not traded")

        signal = self.evaluate(ctx)
        if signal is None:
            return Signal.hold(ctx.product_id, self.name)

        # Spot accounts cannot short: downgrade rather than emit an order the
        # exchange would reject.
        if signal.action is SignalAction.ENTER_SHORT and not ctx.allow_shorts:
            if ctx.is_long:
                return Signal(
                    product_id=ctx.product_id,
                    action=SignalAction.EXIT,
                    strength=signal.strength,
                    strategy=self.name,
                    reason=f"{signal.reason} (short disabled -> flat)",
                )
            return Signal.hold(ctx.product_id, self.name, "short signal, shorts disabled")

        signal.strategy = signal.strategy or self.name
        return signal

    # ------------------------------------------------------------------ #
    # Hooks for subclasses
    # ------------------------------------------------------------------ #

    def observe(self, ctx: StrategyContext) -> None:
        """Update per-bar internal state, called for **every** bar.

        The router invokes this on all loaded strategies before any regime
        gating, which matters for strategies that track a condition building
        over time. A volatility squeeze, for example, resolves *into* a trend —
        if the squeeze strategy only saw bars where the regime was already
        SQUEEZE, it would stop watching at the exact moment its setup fires.

        Default is a no-op; only stateful strategies override it.
        """

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        """Return an entry/exit signal, or ``None`` for "no opinion"."""

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Strategy-specific exit while a position is open.

        Defaults to no opinion; the risk manager's stops always apply on top.
        """
        return None

    def accepts_regime(self, regime: Regime) -> bool:
        return not self.suitable_regimes or regime in self.suitable_regimes

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _long(
        self,
        ctx: StrategyContext,
        strength: float,
        reason: str,
        *,
        stop: Optional[float] = None,
        target: Optional[float] = None,
        **metadata: Any,
    ) -> Signal:
        return self._entry(ctx, SignalAction.ENTER_LONG, strength, reason, stop, target, metadata)

    def _short(
        self,
        ctx: StrategyContext,
        strength: float,
        reason: str,
        *,
        stop: Optional[float] = None,
        target: Optional[float] = None,
        **metadata: Any,
    ) -> Signal:
        return self._entry(ctx, SignalAction.ENTER_SHORT, strength, reason, stop, target, metadata)

    def _entry(
        self,
        ctx: StrategyContext,
        action: SignalAction,
        strength: float,
        reason: str,
        stop: Optional[float],
        target: Optional[float],
        metadata: Dict[str, Any],
    ) -> Signal:
        direction = 1 if action is SignalAction.ENTER_LONG else -1
        stop_price = self._sanitize_stop(ctx, stop, direction)
        return Signal(
            product_id=ctx.product_id,
            action=action,
            strength=clamp(strength, 0.0, 1.0),
            strategy=self.name,
            reason=reason,
            stop_price=stop_price,
            take_profit=to_decimal(target) if target is not None else None,
            metadata=metadata,
        )

    def _exit(self, ctx: StrategyContext, reason: str, strength: float = 1.0) -> Signal:
        return Signal(
            product_id=ctx.product_id,
            action=SignalAction.EXIT,
            strength=clamp(strength, 0.0, 1.0),
            strategy=self.name,
            reason=reason,
        )

    def _sanitize_stop(
        self,
        ctx: StrategyContext,
        stop: Optional[float],
        direction: int,
    ) -> Optional[Decimal]:
        """Reject a stop on the wrong side of price, and cap absurd width.

        A stop above entry on a long would size the position at infinity, so
        this falls back to an ATR stop rather than trusting the caller.
        """
        price = ctx.price
        atr = ctx.features.atr_or()
        if stop is None:
            stop = price - direction * 2.0 * atr
        # Wrong side of the market, or so close it is inside one ATR of noise.
        min_distance = 0.25 * atr
        if direction > 0 and stop >= price - min_distance:
            stop = price - max(min_distance, 1.5 * atr)
        elif direction < 0 and stop <= price + min_distance:
            stop = price + max(min_distance, 1.5 * atr)
        # Never risk more than 15% of price on a single idea.
        max_distance = 0.15 * price
        if abs(price - stop) > max_distance:
            stop = price - direction * max_distance
        return to_decimal(max(1e-9, stop))

    def _atr_stop(self, ctx: StrategyContext, direction: int, multiple: float) -> float:
        return ctx.price - direction * multiple * ctx.features.atr_or()

    def _r_target(self, ctx: StrategyContext, stop: float, direction: int, r_multiple: float) -> float:
        risk = abs(ctx.price - stop)
        return ctx.price + direction * risk * r_multiple

    def describe(self) -> str:
        regimes = ",".join(sorted(r.value for r in self.suitable_regimes)) or "any"
        return f"{self.name}[{regimes}] {self.params}"

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"<{type(self).__name__} {self.name}>"


__all__ = ["Strategy", "StrategyContext"]
