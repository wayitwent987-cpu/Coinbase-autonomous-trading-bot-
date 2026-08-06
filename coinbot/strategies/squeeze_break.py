"""Volatility-squeeze breakout.

When Bollinger bands contract inside the Keltner channels, the market is
coiling: realized volatility has collapsed and a directional expansion tends to
follow. This strategy waits in the squeeze, then takes the first bar that
*expands* out of it, using the direction of the expansion bar rather than
guessing which way the coil resolves.
"""

from __future__ import annotations

from typing import Optional

from ..models import Regime, Signal
from ..util.numeric import clamp
from .base import Strategy, StrategyContext


class SqueezeBreakStrategy(Strategy):
    name = "squeeze_break"
    # Includes the trend regimes on purpose. A squeeze resolves *into* a
    # trend, so the breakout bar usually prints after the classifier has
    # already moved on; excluding trends would gate out the one bar this
    # strategy exists to trade. It is self-gating anyway — nothing fires
    # without a coil that :meth:`observe` actually tracked.
    suitable_regimes = frozenset(
        {Regime.SQUEEZE, Regime.RANGING, Regime.TRENDING_UP, Regime.TRENDING_DOWN}
    )
    min_bars = 100

    defaults = {
        "max_width_rank": 0.25,      # bands must be in the bottom quartile
        "expansion_ratio": 1.20,     # width must widen by this much to trigger
        "min_body_atr": 0.6,         # the expansion bar must be decisive
        "min_volume_ratio": 1.1,
        "stop_atr": 1.8,
        "target_r": 3.0,             # squeezes resolve into big moves; aim wide
        "max_squeeze_age": 60,       # a coil this old has gone stale
        "confirm_with_momentum": True,
    }

    def __init__(self, params: Optional[dict] = None) -> None:
        super().__init__(params)
        # Per-product memory of how long the coil has been building and how
        # tight it got — the trigger is a *change* in width, not a level.
        self._squeeze_bars: dict = {}
        self._min_width: dict = {}
        # Set on the bar a tracked coil ends, and consumed by evaluate().
        self._released: dict = {}

    def observe(self, ctx: StrategyContext) -> None:
        """Track the coil on every bar, whatever the regime says.

        This has to run unconditionally: a squeeze resolves *into* a trend, so
        by the time the breakout bar prints, the regime has often already
        flipped away from SQUEEZE. Watching only "our" regime would mean
        losing the coil exactly when it pays off.
        """
        f, p = ctx.f, self.params
        pid = ctx.product_id
        if f.bb_width is None or not f.is_complete:
            return

        rank = f.bb_width_rank if f.bb_width_rank is not None else 1.0
        coiled = rank <= p["max_width_rank"] or f.in_squeeze

        if coiled:
            self._squeeze_bars[pid] = self._squeeze_bars.get(pid, 0) + 1
            prior = self._min_width.get(pid)
            self._min_width[pid] = f.bb_width if prior is None else min(prior, f.bb_width)
            self._released.pop(pid, None)
            return

        age = self._squeeze_bars.get(pid, 0)
        tightest = self._min_width.get(pid)
        if age > 0 and tightest and tightest > 0:
            # The coil just ended on this bar: hand it to evaluate().
            self._released[pid] = (age, tightest, f.ts)
        self._squeeze_bars[pid] = 0
        self._min_width.pop(pid, None)

    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params
        pid = ctx.product_id

        if f.bb_width is None:
            return None

        released = self._released.get(pid)
        if released is None:
            return None
        age, tightest, ts = released
        if ts != f.ts:
            # Stale: the release happened on an earlier bar and we missed it.
            self._released.pop(pid, None)
            return None

        self._released.pop(pid, None)
        if age > p["max_squeeze_age"]:
            return None
        if f.bb_width < tightest * p["expansion_ratio"]:
            return None  # bands have not actually expanded yet

        atr = f.atr_or()
        body = f.close - f.open
        if abs(body) < p["min_body_atr"] * atr:
            return None  # indecisive bar: direction is not established

        ratio = f.volume_ratio
        if ratio is not None and ratio < p["min_volume_ratio"]:
            return None

        direction = 1 if body > 0 else -1
        if p["confirm_with_momentum"] and f.macd_hist is not None:
            if direction > 0 and f.macd_hist < 0 and not f.macd_rising:
                return None
            if direction < 0 and f.macd_hist > 0 and f.macd_rising:
                return None

        expansion = f.bb_width / tightest
        strength = self._strength(ctx, expansion, age, abs(body) / atr)

        if direction > 0 and not ctx.is_long:
            stop = min(f.open, f.low, f.close - p["stop_atr"] * atr)
            return self._long(
                ctx, strength,
                f"squeeze break up after {age} bars (width x{expansion:.2f})",
                stop=stop, target=self._r_target(ctx, stop, 1, p["target_r"]),
                squeeze_bars=age, expansion=round(expansion, 2),
            )
        if direction < 0 and not ctx.is_short:
            stop = max(f.open, f.high, f.close + p["stop_atr"] * atr)
            return self._short(
                ctx, strength,
                f"squeeze break down after {age} bars (width x{expansion:.2f})",
                stop=stop, target=self._r_target(ctx, stop, -1, p["target_r"]),
                squeeze_bars=age, expansion=round(expansion, 2),
            )
        return None

    def _reset(self, product_id: str) -> None:
        self._squeeze_bars[product_id] = 0
        self._min_width.pop(product_id, None)
        self._released.pop(product_id, None)

    def _strength(self, ctx: StrategyContext, expansion: float, age: int, body_atr: float) -> float:
        p = self.params
        expansion_component = clamp((expansion - p["expansion_ratio"]) / 0.8, 0.0, 1.0)
        # A longer coil stores more energy, up to a point.
        coil_component = clamp(age / 30.0, 0.0, 1.0)
        decisiveness = clamp((body_atr - p["min_body_atr"]) / 1.5, 0.0, 1.0)
        volume_component = clamp(((ctx.f.volume_ratio or 1.0) - 1.0) / 1.5, 0.0, 1.0)
        return clamp(
            0.30 * expansion_component
            + 0.25 * decisiveness
            + 0.25 * coil_component
            + 0.20 * volume_component,
            0.0, 1.0,
        )

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Volatility collapsing again means the expansion failed."""
        f = ctx.f
        if f.bb_width_rank is None:
            return None
        if f.bb_width_rank <= self.params["max_width_rank"] * 0.8:
            return self._exit(ctx, "expansion faded back into a squeeze", 0.8)
        return None


__all__ = ["SqueezeBreakStrategy"]
