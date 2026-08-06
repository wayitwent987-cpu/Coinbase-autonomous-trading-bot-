"""Donchian breakout with confirmation filters.

Naive breakout systems die on false breaks. Three filters do the work here:
the channel window excludes the current bar (so a bar cannot break its own
high), volume must confirm, and the bar has to *close* beyond the level rather
than merely wick through it.
"""

from __future__ import annotations

from typing import Optional

from ..models import Regime, Signal
from ..util.numeric import clamp
from .base import Strategy, StrategyContext


class BreakoutStrategy(Strategy):
    name = "breakout"
    suitable_regimes = frozenset(
        {Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.SQUEEZE, Regime.RANGING}
    )
    min_bars = 80

    defaults = {
        "min_volume_ratio": 1.25,   # bar volume vs its 20-bar average
        "close_beyond_atr": 0.10,   # close must clear the level by this much ATR
        "max_extension_atr": 2.0,   # too far past the level = already chased
        "stop_atr": 2.0,
        "target_r": 2.5,
        "require_volume": True,
        "min_adx": 18.0,
    }

    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params
        if f.donchian_high is None or f.donchian_low is None:
            return None

        atr = f.atr_or()
        buffer = p["close_beyond_atr"] * atr

        volume_ok = True
        if p["require_volume"]:
            ratio = f.volume_ratio
            volume_ok = ratio is not None and ratio >= p["min_volume_ratio"]

        if f.adx is not None and f.adx < p["min_adx"]:
            return None

        # --- Upside break ---
        if f.close > f.donchian_high + buffer and not ctx.is_long:
            if not volume_ok:
                return None
            extension = (f.close - f.donchian_high) / atr
            if extension > p["max_extension_atr"]:
                return None  # the move is already made; entering here is chasing
            if ctx.htf_bias < 0 and ctx.regime.regime is Regime.TRENDING_DOWN:
                return None
            stop = min(f.donchian_high - 0.25 * atr, f.close - p["stop_atr"] * atr)
            target = self._r_target(ctx, stop, 1, p["target_r"])
            return self._long(
                ctx, self._strength(ctx, extension, volume_ok),
                f"broke Donchian high {f.donchian_high:.4f} on {f.volume_ratio or 0:.1f}x volume",
                stop=stop, target=target,
                level=f.donchian_high, extension_atr=round(extension, 2),
            )

        # --- Downside break ---
        if f.close < f.donchian_low - buffer and not ctx.is_short:
            if not volume_ok:
                return None
            extension = (f.donchian_low - f.close) / atr
            if extension > p["max_extension_atr"]:
                return None
            if ctx.htf_bias > 0 and ctx.regime.regime is Regime.TRENDING_UP:
                return None
            stop = max(f.donchian_low + 0.25 * atr, f.close + p["stop_atr"] * atr)
            target = self._r_target(ctx, stop, -1, p["target_r"])
            return self._short(
                ctx, self._strength(ctx, extension, volume_ok),
                f"broke Donchian low {f.donchian_low:.4f} on {f.volume_ratio or 0:.1f}x volume",
                stop=stop, target=target,
                level=f.donchian_low, extension_atr=round(extension, 2),
            )

        return None

    def _strength(self, ctx: StrategyContext, extension: float, volume_ok: bool) -> float:
        f = self.params
        ratio = ctx.f.volume_ratio or 1.0
        volume_component = clamp((ratio - 1.0) / 1.5, 0.0, 1.0)
        # Entering just past the level beats entering two ATR into the move.
        timing = clamp(1.0 - extension / max(0.1, f["max_extension_atr"]), 0.0, 1.0)
        adx_component = clamp(((ctx.f.adx or 0.0) - f["min_adx"]) / 20.0, 0.0, 1.0)
        squeeze_bonus = 0.15 if ctx.regime.regime is Regime.SQUEEZE else 0.0
        return clamp(
            0.30 * volume_component
            + 0.30 * timing
            + 0.20 * adx_component
            + 0.20 * clamp(ctx.regime.confidence, 0.0, 1.0)
            + squeeze_bonus,
            0.0, 1.0,
        )

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Bail when price falls back inside the channel — a failed break."""
        f = ctx.f
        if f.donchian_mid is None:
            return None
        if ctx.is_long and f.close < f.donchian_mid:
            return self._exit(ctx, "failed breakout: back below channel mid", 0.9)
        if ctx.is_short and f.close > f.donchian_mid:
            return self._exit(ctx, "failed breakdown: back above channel mid", 0.9)
        return None


__all__ = ["BreakoutStrategy"]
