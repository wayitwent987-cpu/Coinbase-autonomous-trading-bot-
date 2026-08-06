"""Range scalping: buy the floor, sell the ceiling of an established range.

Where mean reversion fades statistical stretch, this trades *structure* — an
explicit range built from recent swing highs and lows. It only acts when the
range is well-formed (wide enough to pay for fees and slippage, and respected
by recent price action), and it takes profit at the opposite edge rather than
waiting for a trend that a ranging market will not deliver.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..models import Regime, Signal
from ..util.numeric import clamp, safe_div
from .base import Strategy, StrategyContext


class RangeScalpStrategy(Strategy):
    name = "range_scalp"
    suitable_regimes = frozenset({Regime.RANGING})
    min_bars = 80

    defaults = {
        "range_lookback": 48,
        "edge_zone": 0.20,          # act within the outer 20% of the range
        "min_range_atr": 3.0,       # range must be worth trading
        "min_range_pct": 0.010,     # ...and at least 1% wide after costs
        "max_adx": 25.0,
        "stop_atr": 1.0,            # tight: the range edge is the invalidation
        "target_fraction": 0.75,    # exit at 75% of the way across the range
        "min_touches": 2,           # edges must have been respected before
    }

    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params

        if f.adx is not None and f.adx > p["max_adx"]:
            return None

        bounds = self._range_bounds(ctx)
        if bounds is None:
            return None
        low, high = bounds

        span = high - low
        atr = f.atr_or()
        if span < p["min_range_atr"] * atr:
            return None
        if safe_div(span, f.close, 0.0) < p["min_range_pct"]:
            return None

        position_in_range = safe_div(f.close - low, span, 0.5)

        # --- Near the floor: buy ---
        if position_in_range <= p["edge_zone"] and not ctx.is_long:
            if self._touches(ctx, low, is_low=True) < p["min_touches"]:
                return None
            stop = min(low - p["stop_atr"] * atr, f.close - 0.5 * atr)
            target = low + span * p["target_fraction"]
            return self._long(
                ctx, self._strength(ctx, position_in_range, span, atr, is_long=True),
                f"range floor {low:.4f} (in-range {position_in_range:.0%})",
                stop=stop, target=target, range_low=low, range_high=high,
            )

        # --- Near the ceiling: sell / close ---
        if position_in_range >= 1.0 - p["edge_zone"]:
            if ctx.is_long:
                return self._exit(ctx, f"range ceiling {high:.4f} reached")
            if not ctx.is_short:
                if self._touches(ctx, high, is_low=False) < p["min_touches"]:
                    return None
                stop = max(high + p["stop_atr"] * atr, f.close + 0.5 * atr)
                target = high - span * p["target_fraction"]
                return self._short(
                    ctx, self._strength(ctx, position_in_range, span, atr, is_long=False),
                    f"range ceiling {high:.4f} (in-range {position_in_range:.0%})",
                    stop=stop, target=target, range_low=low, range_high=high,
                )

        return None

    # ------------------------------------------------------------------ #

    def _range_bounds(self, ctx: StrategyContext) -> Optional[Tuple[float, float]]:
        """The recent high/low envelope, excluding the current bar."""
        candles = ctx.f.candles
        lookback = self.params["range_lookback"]
        if len(candles) < lookback + 2:
            return None
        window = candles[-lookback - 1: -1]
        high = max(c.high for c in window)
        low = min(c.low for c in window)
        if high <= low:
            return None
        return low, high

    def _touches(self, ctx: StrategyContext, level: float, *, is_low: bool) -> int:
        """How many times price has respected this edge recently.

        An edge tested twice is a range; an edge tested once is just the last
        swing, and trading it is guessing.
        """
        candles = ctx.f.candles
        lookback = self.params["range_lookback"]
        window = candles[-lookback - 1: -1]
        if not window:
            return 0
        tolerance = ctx.f.atr_or() * 0.6
        if is_low:
            return sum(1 for c in window if c.low <= level + tolerance)
        return sum(1 for c in window if c.high >= level - tolerance)

    def _strength(
        self,
        ctx: StrategyContext,
        position_in_range: float,
        span: float,
        atr: float,
        *,
        is_long: bool,
    ) -> float:
        p = self.params
        # Closer to the edge = better entry.
        edge_quality = clamp(
            1.0 - position_in_range / max(1e-9, p["edge_zone"])
            if is_long
            else 1.0 - (1.0 - position_in_range) / max(1e-9, p["edge_zone"]),
            0.0, 1.0,
        )
        # A wider range gives more room for the target to pay.
        room = clamp((span / atr - p["min_range_atr"]) / 6.0, 0.0, 1.0)
        no_trend = clamp(1.0 - (ctx.f.adx or 0.0) / max(1.0, p["max_adx"]), 0.0, 1.0)
        return clamp(
            0.35 * edge_quality
            + 0.25 * room
            + 0.20 * no_trend
            + 0.20 * clamp(ctx.regime.confidence, 0.0, 1.0),
            0.0, 1.0,
        )

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Exit if the range breaks — the premise of the trade is gone."""
        bounds = self._range_bounds(ctx)
        if bounds is None:
            return None
        low, high = bounds
        f = ctx.f
        atr = f.atr_or()
        if ctx.is_long and f.close < low - 0.5 * atr:
            return self._exit(ctx, f"range broke below {low:.4f}")
        if ctx.is_short and f.close > high + 0.5 * atr:
            return self._exit(ctx, f"range broke above {high:.4f}")
        return None


__all__ = ["RangeScalpStrategy"]
