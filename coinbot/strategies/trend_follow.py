"""Trend following: ride an established trend, entering on pullbacks.

Buying the moment an EMA crosses is how trend systems get chopped up — the
cross is the most crowded, least favourable price. This enters on a *pullback
into* an already-confirmed trend instead, which puts the stop closer and makes
the reward-to-risk work.
"""

from __future__ import annotations

from typing import Optional

from .. import indicators as ind
from ..models import Regime, Signal
from ..util.numeric import clamp
from .base import Strategy, StrategyContext


class TrendFollowStrategy(Strategy):
    name = "trend_follow"
    suitable_regimes = frozenset({Regime.TRENDING_UP, Regime.TRENDING_DOWN})
    min_bars = 120

    defaults = {
        "adx_min": 22.0,
        "pullback_atr": 1.5,       # how close to the fast EMA counts as a pullback
        "rsi_long_floor": 40.0,    # don't buy a trend that has broken down
        "rsi_long_ceiling": 82.0,  # don't chase a blow-off
        "rsi_short_ceiling": 60.0,
        "rsi_short_floor": 18.0,
        "stop_atr": 2.2,
        "target_r": 2.5,
        "require_htf": True,
        "exit_on_cross": True,
        # A trend system that can only buy pullbacks never joins a new trend:
        # in a smooth advance price simply never comes back to the fast EMA.
        # The cross path catches the start; the pullback path adds later.
        "allow_cross_entry": True,
        "cross_lookback": 4,          # bars since the cross that still count
        "cross_max_extension_atr": 3.0,
        # Must match the feature pipeline's EMA periods for the cross lookback
        # to describe the same lines the rest of the strategy reasons about.
        "ema_fast_period": 21,
        "ema_slow_period": 55,
    }

    def evaluate(self, ctx: StrategyContext) -> Optional[Signal]:
        f = ctx.f
        p = self.params

        if f.adx is not None and f.adx < p["adx_min"]:
            return None
        if f.ema_fast is None or f.ema_slow is None:
            return None

        uptrend = f.ema_fast > f.ema_slow and (f.above_trend is not False)
        downtrend = f.ema_fast < f.ema_slow and (f.above_trend is not True)

        if uptrend and not ctx.is_long:
            return self._long_setup(ctx)
        if downtrend and not ctx.is_short:
            return self._short_setup(ctx)
        return None

    # ------------------------------------------------------------------ #

    def _long_setup(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params

        if p["require_htf"] and ctx.htf_bias < 0:
            return None
        if f.rsi is None or not (p["rsi_long_floor"] <= f.rsi <= p["rsi_long_ceiling"]):
            return None

        atr = f.atr_or()
        # A pullback: price dipped toward the fast EMA and is turning back up.
        distance = (f.close - f.ema_fast) / atr
        pulled_back = distance <= p["pullback_atr"]
        resuming = f.close > f.open or f.macd_rising

        entry_kind = ""
        if pulled_back and resuming:
            entry_kind = "pullback"
        elif (
            p["allow_cross_entry"]
            and self._crossed_recently(ctx, up=True)
            and distance <= p["cross_max_extension_atr"]
        ):
            entry_kind = "fresh cross"
        else:
            return None

        # Stop below the pullback low, not just a fixed ATR from price — the
        # structure is what has to break for the trade to be wrong.
        swing = f.swing_low if f.swing_low is not None else f.low
        structure_stop = min(swing, f.close - p["stop_atr"] * atr)
        stop = min(structure_stop, f.close - 0.8 * atr)
        target = self._r_target(ctx, stop, 1, p["target_r"])

        strength = self._strength(ctx, distance, is_long=True)
        return self._long(
            ctx, strength,
            f"uptrend {entry_kind} (adx={f.adx:.0f}, {distance:.1f} ATR above fast EMA)",
            stop=stop, target=target, adx=f.adx,
            extension_atr=round(distance, 2), entry=entry_kind,
        )

    def _short_setup(self, ctx: StrategyContext) -> Optional[Signal]:
        f, p = ctx.f, self.params

        if p["require_htf"] and ctx.htf_bias > 0:
            return None
        if f.rsi is None or not (p["rsi_short_floor"] <= f.rsi <= p["rsi_short_ceiling"]):
            return None

        atr = f.atr_or()
        distance = (f.ema_fast - f.close) / atr
        pulled_back = distance <= p["pullback_atr"]
        resuming = f.close < f.open or (f.macd_hist is not None and not f.macd_rising)

        entry_kind = ""
        if pulled_back and resuming:
            entry_kind = "pullback"
        elif (
            p["allow_cross_entry"]
            and self._crossed_recently(ctx, up=False)
            and distance <= p["cross_max_extension_atr"]
        ):
            entry_kind = "fresh cross"
        else:
            return None

        swing = f.swing_high if f.swing_high is not None else f.high
        structure_stop = max(swing, f.close + p["stop_atr"] * atr)
        stop = max(structure_stop, f.close + 0.8 * atr)
        target = self._r_target(ctx, stop, -1, p["target_r"])

        strength = self._strength(ctx, distance, is_long=False)
        return self._short(
            ctx, strength,
            f"downtrend {entry_kind} (adx={f.adx:.0f}, {distance:.1f} ATR below fast EMA)",
            stop=stop, target=target, adx=f.adx,
            extension_atr=round(distance, 2), entry=entry_kind,
        )

    def _crossed_recently(self, ctx: StrategyContext, *, up: bool) -> bool:
        """Did the fast EMA cross the slow one within ``cross_lookback`` bars?

        Recomputed from raw closes rather than read off the FeatureSet, which
        only carries the current bar and the one before it. Being stateless
        keeps the answer identical whether the caller is replaying history in
        the backtester or running live.
        """
        lookback = int(self.params["cross_lookback"])
        closes = [c.close for c in ctx.f.candles]
        fast_period = int(self.params["ema_fast_period"])
        slow_period = int(self.params["ema_slow_period"])
        if len(closes) < slow_period + lookback + 2:
            return False

        fast = ind.ema(closes, fast_period)
        slow = ind.ema(closes, slow_period)
        window = [
            (f, s)
            for f, s in zip(fast[-(lookback + 2):], slow[-(lookback + 2):])
            if f is not None and s is not None
        ]
        for (prev_f, prev_s), (cur_f, cur_s) in zip(window, window[1:]):
            if up and prev_f <= prev_s and cur_f > cur_s:
                return True
            if not up and prev_f >= prev_s and cur_f < cur_s:
                return True
        return False

    def _strength(self, ctx: StrategyContext, distance: float, *, is_long: bool) -> float:
        """Conviction from trend strength, HTF agreement, and entry quality."""
        f = ctx.f
        adx_component = clamp(((f.adx or 0.0) - self.params["adx_min"]) / 20.0, 0.0, 1.0)
        # A shallower pullback (closer to the EMA) is a better entry.
        entry_quality = clamp(1.0 - abs(distance) / max(0.1, self.params["pullback_atr"]), 0.0, 1.0)
        htf_component = 1.0 if ctx.htf_bias == (1 if is_long else -1) else 0.5
        regime_component = clamp(ctx.regime.confidence, 0.0, 1.0)
        return clamp(
            0.35 * adx_component
            + 0.25 * entry_quality
            + 0.20 * htf_component
            + 0.20 * regime_component,
            0.0, 1.0,
        )

    # ------------------------------------------------------------------ #

    def evaluate_exit(self, ctx: StrategyContext) -> Optional[Signal]:
        """Leave when the trend that justified the trade stops existing."""
        if not self.params["exit_on_cross"]:
            return None
        f = ctx.f
        if f.ema_fast is None or f.ema_slow is None:
            return None

        if ctx.is_long and f.ema_cross_down:
            return self._exit(ctx, "trend exit: fast EMA crossed below slow")
        if ctx.is_short and f.ema_cross_up:
            return self._exit(ctx, "trend exit: fast EMA crossed above slow")

        # Trend strength collapsing is an exit even without a cross.
        if f.adx is not None and f.adx < self.params["adx_min"] * 0.6:
            return self._exit(ctx, f"trend exit: adx faded to {f.adx:.0f}")
        return None


__all__ = ["TrendFollowStrategy"]
