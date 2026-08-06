"""One indicator pass per bar, shared by the regime detector and every strategy.

Recomputing ADX five times because five strategies want it is the easiest way
to make the loop slow, so features are computed once into an immutable
snapshot. Each field holds the value at the **last closed bar**, and ``*_prev``
fields hold the bar before it so strategies can detect crosses without
re-deriving series.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .. import indicators as ind
from ..config import RegimeConfig
from ..models import Candle
from ..util.numeric import safe_div


@dataclass(frozen=True)
class FeatureParams:
    """Indicator periods. Defaults match the regime config's expectations."""

    ema_fast: int = 21
    ema_slow: int = 55
    ema_trend: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    kc_ema: int = 20
    kc_atr: int = 10
    kc_mult: float = 1.5
    donchian_period: int = 20
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_k: int = 14
    stoch_d: int = 3
    vol_period: int = 20
    slope_period: int = 20
    width_rank_lookback: int = 96
    roc_short: int = 6
    roc_long: int = 24
    swing_lookback: int = 20
    bars_per_year: int = 105_120

    @classmethod
    def from_regime_config(cls, cfg: RegimeConfig, bars_per_year: int) -> "FeatureParams":
        return cls(
            ema_fast=cfg.trend_fast_ema,
            ema_slow=cfg.trend_slow_ema,
            ema_trend=cfg.trend_confirm_ema,
            atr_period=cfg.atr_period,
            adx_period=cfg.adx_period,
            bb_period=cfg.bb_period,
            bb_std=cfg.bb_std,
            width_rank_lookback=cfg.vol_lookback,
            bars_per_year=bars_per_year,
        )

    @property
    def min_bars(self) -> int:
        """Bars needed before every field can be populated."""
        return max(
            self.ema_trend,
            self.adx_period * 3,
            self.bb_period + self.width_rank_lookback // 2,
            self.macd_slow + self.macd_signal,
        ) + 2


@dataclass(frozen=True)
class FeatureSet:
    """Indicator snapshot at the most recent closed bar."""

    product_id: str
    ts: int
    n_bars: int

    # Price
    close: float
    open: float
    high: float
    low: float
    close_prev: float

    # Trend
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_trend: Optional[float] = None
    ema_fast_prev: Optional[float] = None
    ema_slow_prev: Optional[float] = None
    slope: Optional[float] = None
    supertrend_dir: Optional[int] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None

    # Momentum
    rsi: Optional[float] = None
    rsi_prev: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_prev: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    roc_short: Optional[float] = None
    roc_long: Optional[float] = None

    # Volatility / channels
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width: Optional[float] = None
    bb_width_rank: Optional[float] = None
    percent_b: Optional[float] = None
    percent_b_prev: Optional[float] = None
    kc_upper: Optional[float] = None
    kc_lower: Optional[float] = None
    donchian_high: Optional[float] = None
    donchian_low: Optional[float] = None
    donchian_mid: Optional[float] = None
    realized_vol: Optional[float] = None

    # Volume / structure
    volume: float = 0.0
    volume_ratio: Optional[float] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None

    # Raw history, for strategies that need bar-level detail.
    candles: Sequence[Candle] = field(default=(), repr=False)

    # ------------------------------------------------------------------ #
    # Derived convenience properties
    # ------------------------------------------------------------------ #

    @property
    def is_complete(self) -> bool:
        """True when the fields every strategy relies on are populated."""
        return None not in (self.ema_fast, self.ema_slow, self.rsi, self.atr, self.bb_mid)

    @property
    def ema_cross_up(self) -> bool:
        if None in (self.ema_fast, self.ema_slow, self.ema_fast_prev, self.ema_slow_prev):
            return False
        return self.ema_fast_prev <= self.ema_slow_prev and self.ema_fast > self.ema_slow  # type: ignore[operator]

    @property
    def ema_cross_down(self) -> bool:
        if None in (self.ema_fast, self.ema_slow, self.ema_fast_prev, self.ema_slow_prev):
            return False
        return self.ema_fast_prev >= self.ema_slow_prev and self.ema_fast < self.ema_slow  # type: ignore[operator]

    @property
    def above_trend(self) -> Optional[bool]:
        if self.ema_trend is None:
            return None
        return self.close > self.ema_trend

    @property
    def macd_rising(self) -> bool:
        if self.macd_hist is None or self.macd_hist_prev is None:
            return False
        return self.macd_hist > self.macd_hist_prev

    @property
    def in_squeeze(self) -> bool:
        """Bollinger bands inside Keltner channels."""
        if None in (self.bb_upper, self.bb_lower, self.kc_upper, self.kc_lower):
            return False
        return self.bb_upper < self.kc_upper and self.bb_lower > self.kc_lower  # type: ignore[operator]

    def atr_or(self, fallback_pct: float = 0.01) -> float:
        """ATR, or a percentage of price when ATR is unavailable.

        Sizing and stops must never divide by zero just because warm-up is
        incomplete, so this always returns something usable.
        """
        if self.atr and self.atr > 0:
            return self.atr
        return max(1e-12, self.close * fallback_pct)

    def distance_to(self, level: Optional[float]) -> Optional[float]:
        """Signed distance from close to a level, in ATR multiples."""
        if level is None:
            return None
        return (self.close - level) / self.atr_or()


def compute_features(
    product_id: str,
    candles: Sequence[Candle],
    params: Optional[FeatureParams] = None,
) -> Optional[FeatureSet]:
    """Compute the snapshot for the newest bar in ``candles``.

    Returns ``None`` if there is not enough history for even the basic fields;
    callers treat that as "stay flat and keep warming up".
    """
    params = params or FeatureParams()
    n = len(candles)
    if n < 2:
        return None

    closes = [c.close for c in candles]
    latest = candles[-1]
    i = n - 1

    def at(series: Sequence[Optional[float]], offset: int = 0) -> Optional[float]:
        idx = i - offset
        if idx < 0 or idx >= len(series):
            return None
        return series[idx]

    ema_fast_s = ind.ema(closes, params.ema_fast)
    ema_slow_s = ind.ema(closes, params.ema_slow)
    ema_trend_s = ind.ema(closes, params.ema_trend)
    rsi_s = ind.rsi(closes, params.rsi_period)
    atr_s = ind.atr(candles, params.atr_period)
    _plus_di, _minus_di, _adx = ind.adx(candles, params.adx_period)
    bb_up, bb_mid, bb_lo, bb_width, pct_b = ind.bollinger(closes, params.bb_period, params.bb_std)
    kc_up, _kc_mid, kc_lo = ind.keltner(candles, params.kc_ema, params.kc_atr, params.kc_mult)
    dc_up, dc_lo, dc_mid = ind.donchian(candles, params.donchian_period)
    macd_line, macd_sig, macd_hist = ind.macd(
        closes, params.macd_fast, params.macd_slow, params.macd_signal
    )
    stoch_k_s, stoch_d_s = ind.stochastic(candles, params.stoch_k, params.stoch_d)
    slope_s = ind.linreg_slope(closes, params.slope_period)
    vol_ratio_s = ind.volume_ratio(candles, params.vol_period)
    rvol_s = ind.realized_vol(closes, min(params.width_rank_lookback, 30), params.bars_per_year)
    width_rank_s = ind.percentile_rank(bb_width, params.width_rank_lookback)
    _st_line, st_dir = ind.supertrend(candles, params.kc_atr, 3.0)
    roc_s_s = ind.roc(closes, params.roc_short)
    roc_l_s = ind.roc(closes, params.roc_long)

    swing_window = candles[-params.swing_lookback:] if n >= 2 else candles
    swing_high = max((c.high for c in swing_window), default=None)
    swing_low = min((c.low for c in swing_window), default=None)

    atr_now = at(atr_s)
    atr_pct = safe_div(atr_now, latest.close, 0.0) if atr_now is not None else None

    return FeatureSet(
        product_id=product_id,
        ts=latest.start,
        n_bars=n,
        close=latest.close,
        open=latest.open,
        high=latest.high,
        low=latest.low,
        close_prev=candles[-2].close,
        ema_fast=at(ema_fast_s),
        ema_slow=at(ema_slow_s),
        ema_trend=at(ema_trend_s),
        ema_fast_prev=at(ema_fast_s, 1),
        ema_slow_prev=at(ema_slow_s, 1),
        slope=at(slope_s),
        supertrend_dir=st_dir[i] if i < len(st_dir) else None,
        adx=at(_adx),
        plus_di=at(_plus_di),
        minus_di=at(_minus_di),
        rsi=at(rsi_s),
        rsi_prev=at(rsi_s, 1),
        macd=at(macd_line),
        macd_signal=at(macd_sig),
        macd_hist=at(macd_hist),
        macd_hist_prev=at(macd_hist, 1),
        stoch_k=at(stoch_k_s),
        stoch_d=at(stoch_d_s),
        roc_short=at(roc_s_s),
        roc_long=at(roc_l_s),
        atr=atr_now,
        atr_pct=atr_pct,
        bb_upper=at(bb_up),
        bb_mid=at(bb_mid),
        bb_lower=at(bb_lo),
        bb_width=at(bb_width),
        bb_width_rank=at(width_rank_s),
        percent_b=at(pct_b),
        percent_b_prev=at(pct_b, 1),
        kc_upper=at(kc_up),
        kc_lower=at(kc_lo),
        donchian_high=at(dc_up),
        donchian_low=at(dc_lo),
        donchian_mid=at(dc_mid),
        realized_vol=at(rvol_s),
        volume=latest.volume,
        volume_ratio=at(vol_ratio_s),
        swing_high=swing_high,
        swing_low=swing_low,
        candles=candles,
    )


def adx_snapshot(candles: Sequence[Candle], period: int = 14) -> tuple:
    """``(+DI, -DI, ADX)`` at the newest bar — the regime detector's input."""
    plus_di, minus_di, adx_series = ind.adx(candles, period)
    return (
        ind.last_value(plus_di),
        ind.last_value(minus_di),
        ind.last_value(adx_series),
    )


def annualization_factor(bar_seconds: int) -> int:
    """Bars per year for a given bar length, for volatility annualization."""
    if bar_seconds <= 0:
        raise ValueError("bar_seconds must be positive")
    return max(1, int(math.floor(365 * 24 * 3600 / bar_seconds)))


__all__ = [
    "FeatureParams",
    "FeatureSet",
    "adx_snapshot",
    "annualization_factor",
    "compute_features",
]
