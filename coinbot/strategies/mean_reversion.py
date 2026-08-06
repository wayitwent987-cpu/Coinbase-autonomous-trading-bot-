"""Mean reversion: fade stretched moves back toward the middle of the range.

The danger with buying oversold is that "oversold" is exactly what a downtrend
looks like all the way down. Two guards apply: the strategy refuses to fade a
strongly trending tape (ADX gate), and it waits for evidence the move has
actually stopped — an RSI upturn or a close back inside the band — instead of
catching the knife mid-fall.
"""

from __future__ import annotations

from typing import Optional

from ..models import Regime, Signal
from ..util.numeric import clamp
from .base import Strategy, StrategyContext


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    suitable_regimes = frozenset({Regime.RANGING, Regime.VOLATILE_CHOP})
    min_bars = 80

    defaults = {
        "rsi_oversold": 30.0,
        "rsi_overbought": 70.0,
        "percent_b_low": 0.05,      # at/below the lower Bollinger band
        "percent_b_high": 0.95,
        "max_adx": 30.0,            # above this the tape is trending: don't fade
        "require_reversal_bar": True,
        "setup_valid_bars": 3,      # how long a stretch stays actionable
        "stop_atr": 1.8,
        "target": "middle_band",    # middle_band | opposite_band | r_multiple
        "target_r": 1.5,
        "max_vol_pct": 0.09,        # refuse to fade a genuinely broken market
    }

    def __init__(self, params: Optional[dict] = None) -> None:
        super().__init__(params)
        # Bars since price was last stretched, per product and direction.
        self._stretch_low: dict = {}
        self._stretch_high: dict = {}

    def observe(self, ctx: StrategyContext) -> None:
        """Remember recent stretches so the trigger can arrive a bar later.

        Price hits the band and reverses on *different* bars: on the stretch
        bar itself RSI is still falling and the candle is red, so demanding
        both at once means the setup never fires. Recording the stretch and
        acting when the turn shows up is what makes this tradeable.
        """
        f, p = ctx.f, self.params
        if f.percent_b is None or f.rsi is None:
            return
        pid = ctx.product_id
        window = int(p["setup_valid_bars"])

        if f.percent_b <= p["percent_b_low"] or f.rsi <= p["rsi_oversold"]:
            self._stretch_low[pid] = window
        elif self._stretch_low.get(pid, 0) > 0:
            self._stretch_low[pid] -= 1

        if f.percent_b >= p["percent_b_high"] or f.rsi >= p["rsi_overbought"]:
            self._stretch_high[pid] = window
        elif self._stretch_high.get(pid, 0) > 0:
            self._stretch_high[pid] -= 1

    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params
        pid = ctx.product_id

        if f.percent_b is None or f.rsi is None or f.bb_mid is None:
            return None
        # Fading a strong trend is the fastest way to lose money doing this.
        if f.adx is not None and f.adx > p["max_adx"]:
            return None
        if f.atr_pct is not None and f.atr_pct > p["max_vol_pct"]:
            return None

        atr = f.atr_or()

        # --- Oversold: buy the lower band ---
        stretched_down = self._stretch_low.get(pid, 0) > 0
        if stretched_down and not ctx.is_long:
            if p["require_reversal_bar"] and not self._turning_up(ctx):
                return None
            stop = min(f.low, f.close - p["stop_atr"] * atr)
            target = self._target_for(ctx, stop, direction=1)
            return self._long(
                ctx, self._strength(ctx, is_long=True),
                f"oversold snap-back (rsi={f.rsi:.0f}, %B={f.percent_b:.2f})",
                stop=stop, target=target, rsi=f.rsi, percent_b=round(f.percent_b, 3),
            )

        # --- Overbought: fade the upper band ---
        stretched_up = self._stretch_high.get(pid, 0) > 0
        if stretched_up and not ctx.is_short:
            if ctx.is_long:
                # Even without shorting, an overbought reading closes a long.
                return self._exit(ctx, f"mean-reversion target: overbought (rsi={f.rsi:.0f})")
            if p["require_reversal_bar"] and not self._turning_down(ctx):
                return None
            stop = max(f.high, f.close + p["stop_atr"] * atr)
            target = self._target_for(ctx, stop, direction=-1)
            return self._short(
                ctx, self._strength(ctx, is_long=False),
                f"overbought fade (rsi={f.rsi:.0f}, %B={f.percent_b:.2f})",
                stop=stop, target=target, rsi=f.rsi, percent_b=round(f.percent_b, 3),
            )

        return None

    # ------------------------------------------------------------------ #

    def _turning_up(self, ctx: StrategyContext) -> bool:
        """Evidence the fall has paused: RSI ticking up, or a bullish close."""
        f = ctx.f
        rsi_up = f.rsi is not None and f.rsi_prev is not None and f.rsi > f.rsi_prev
        back_inside = (
            f.percent_b is not None
            and f.percent_b_prev is not None
            and f.percent_b_prev < 0.0 <= f.percent_b
        )
        return rsi_up or back_inside or (f.close > f.open and f.close > f.close_prev)

    def _turning_down(self, ctx: StrategyContext) -> bool:
        f = ctx.f
        rsi_down = f.rsi is not None and f.rsi_prev is not None and f.rsi < f.rsi_prev
        back_inside = (
            f.percent_b is not None
            and f.percent_b_prev is not None
            and f.percent_b_prev > 1.0 >= f.percent_b
        )
        return rsi_down or back_inside or (f.close < f.open and f.close < f.close_prev)

    def _target_for(self, ctx: StrategyContext, stop: float, direction: int) -> float:
        f, mode = ctx.f, self.params["target"]
        if mode == "middle_band" and f.bb_mid is not None:
            return f.bb_mid
        if mode == "opposite_band":
            level = f.bb_upper if direction > 0 else f.bb_lower
            if level is not None:
                return level
        return self._r_target(ctx, stop, direction, self.params["target_r"])

    def _strength(self, ctx: StrategyContext, *, is_long: bool) -> float:
        f, p = ctx.f, self.params
        rsi = f.rsi or 50.0
        if is_long:
            stretch = clamp((p["rsi_oversold"] - rsi) / 20.0, 0.0, 1.0)
        else:
            stretch = clamp((rsi - p["rsi_overbought"]) / 20.0, 0.0, 1.0)
        band_stretch = 0.0
        if f.percent_b is not None:
            band_stretch = clamp(
                (0.0 - f.percent_b) / 0.25 if is_long else (f.percent_b - 1.0) / 0.25,
                0.0, 1.0,
            )
        # Mean reversion works best when there is no trend to fight.
        no_trend = clamp(1.0 - (f.adx or 0.0) / max(1.0, p["max_adx"]), 0.0, 1.0)
        return clamp(
            0.30 * stretch
            + 0.25 * band_stretch
            + 0.25 * no_trend
            + 0.20 * clamp(ctx.regime.confidence, 0.0, 1.0),
            0.0, 1.0,
        )

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Take the trade off once price has reverted to the mean."""
        f = ctx.f
        if f.bb_mid is None:
            return None
        if ctx.is_long and f.close >= f.bb_mid:
            return self._exit(ctx, "reverted to middle band")
        if ctx.is_short and f.close <= f.bb_mid:
            return self._exit(ctx, "reverted to middle band")
        # If a trend has since developed, the premise is gone.
        if f.adx is not None and f.adx > self.params["max_adx"] * 1.3:
            return self._exit(ctx, f"range broke into trend (adx={f.adx:.0f})", 0.9)
        return None


__all__ = ["MeanReversionStrategy"]
