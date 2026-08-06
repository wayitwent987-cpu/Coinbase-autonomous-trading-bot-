"""Pure-Python technical indicators.

Design rules that every function here follows:

* Input is a sequence of ``float`` (or ``Candle`` where OHLC is needed).
* Output is a list of the **same length** as the input.
* Positions that cannot be computed yet hold ``None`` rather than a
  placeholder number. Warm-up is explicit, so a strategy can never silently
  trade on a half-formed indicator.

Smoothing follows Wilder's convention for RSI/ATR/ADX, which is what charting
packages and the wider literature use — values here line up with TradingView.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..models import Candle
from ..util.numeric import safe_div

Series = List[Optional[float]]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def last_value(series: Sequence[Optional[float]]) -> Optional[float]:
    """Most recent non-``None`` value, or ``None`` if the series is all warm-up."""
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def _require(period: int, name: str = "period") -> None:
    if period < 1:
        raise ValueError(f"{name} must be >= 1, got {period}")


# --------------------------------------------------------------------------- #
# Moving averages and dispersion
# --------------------------------------------------------------------------- #


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average, computed with a rolling sum (O(n))."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    window = math.fsum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first window."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = math.fsum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def wilder_smooth(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing (an EMA with alpha = 1/period), seeded with a sum."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    total = math.fsum(values[:period])
    out[period - 1] = total
    for i in range(period, len(values)):
        total = total - (total / period) + values[i]
        out[i] = total
    return out


def stdev(values: Sequence[float], period: int, *, sample: bool = False) -> Series:
    """Rolling standard deviation (population by default, as Bollinger uses)."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period or period < 2:
        return out
    denom = period - 1 if sample else period
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        mean = math.fsum(window) / period
        var = math.fsum((v - mean) ** 2 for v in window) / denom
        out[i] = math.sqrt(max(0.0, var))
    return out


def zscore(values: Sequence[float], period: int) -> Series:
    """How many rolling standard deviations the latest value sits from its mean."""
    means = sma(values, period)
    devs = stdev(values, period)
    out: Series = [None] * len(values)
    for i, (mean, dev) in enumerate(zip(means, devs)):
        if mean is None or dev is None or dev == 0:
            continue
        out[i] = (values[i] - mean) / dev
    return out


def roc(values: Sequence[float], period: int) -> Series:
    """Rate of change over ``period`` bars, as a fraction (0.05 = +5%)."""
    _require(period)
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        prior = values[i - period]
        if prior:
            out[i] = (values[i] - prior) / abs(prior)
    return out


def linreg_slope(values: Sequence[float], period: int) -> Series:
    """Least-squares slope over the window, normalized by price.

    Normalizing turns the slope into "fraction of price gained per bar", which
    is comparable across BTC at $60k and DOGE at $0.10.
    """
    _require(period)
    out: Series = [None] * len(values)
    if period < 2:
        return out
    xs = list(range(period))
    x_mean = (period - 1) / 2.0
    x_var = math.fsum((x - x_mean) ** 2 for x in xs)
    if x_var == 0:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        y_mean = math.fsum(window) / period
        cov = math.fsum((xs[j] - x_mean) * (window[j] - y_mean) for j in range(period))
        slope = cov / x_var
        out[i] = safe_div(slope, abs(y_mean), 0.0)
    return out


# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Wilder's Relative Strength Index, 0-100."""
    _require(period)
    n = len(values)
    out: Series = [None] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[Series, Series, Series]:
    """Returns ``(macd_line, signal_line, histogram)``."""
    if fast >= slow:
        raise ValueError("macd fast period must be < slow period")
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    n = len(values)
    line: Series = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            line[i] = fast_ema[i] - slow_ema[i]  # type: ignore[operator]

    # The signal EMA runs on the MACD line's defined region only.
    defined = [(i, v) for i, v in enumerate(line) if v is not None]
    sig: Series = [None] * n
    hist: Series = [None] * n
    if defined:
        sig_vals = ema([v for _, v in defined], signal)
        for (idx, _), sv in zip(defined, sig_vals):
            sig[idx] = sv
    for i in range(n):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]  # type: ignore[operator]
    return line, sig, hist


def stochastic(
    candles: Sequence[Candle],
    k_period: int = 14,
    d_period: int = 3,
    smooth_k: int = 3,
) -> Tuple[Series, Series]:
    """Slow stochastic oscillator: ``(%K, %D)``, both 0-100."""
    _require(k_period)
    n = len(candles)
    raw_k: Series = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1: i + 1]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        span = hi - lo
        raw_k[i] = 50.0 if span == 0 else (candles[i].close - lo) / span * 100.0

    k = _smooth_optional(raw_k, smooth_k)
    d = _smooth_optional(k, d_period)
    return k, d


def williams_r(candles: Sequence[Candle], period: int = 14) -> Series:
    """Williams %R, -100 (oversold) to 0 (overbought)."""
    _require(period)
    n = len(candles)
    out: Series = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1: i + 1]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        span = hi - lo
        out[i] = -50.0 if span == 0 else (hi - candles[i].close) / span * -100.0
    return out


def _smooth_optional(series: Sequence[Optional[float]], period: int) -> Series:
    """SMA over a series that has ``None`` warm-up entries."""
    if period <= 1:
        return list(series)
    n = len(series)
    out: Series = [None] * n
    for i in range(n):
        window = series[max(0, i - period + 1): i + 1]
        if len(window) < period or any(v is None for v in window):
            continue
        out[i] = math.fsum(v for v in window if v is not None) / period
    return out


# --------------------------------------------------------------------------- #
# Volatility and range
# --------------------------------------------------------------------------- #


def true_range(candles: Sequence[Candle]) -> List[float]:
    """True range per bar; the first bar falls back to high-low."""
    out: List[float] = []
    for i, candle in enumerate(candles):
        if i == 0:
            out.append(candle.high - candle.low)
            continue
        prev_close = candles[i - 1].close
        out.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    """Average True Range with Wilder smoothing."""
    _require(period)
    trs = true_range(candles)
    n = len(trs)
    out: Series = [None] * n
    if n < period:
        return out
    prev = math.fsum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def adx(
    candles: Sequence[Candle],
    period: int = 14,
) -> Tuple[Series, Series, Series]:
    """Wilder's directional system: ``(+DI, -DI, ADX)``.

    ADX measures trend *strength* regardless of direction; +DI/-DI give the
    direction. The regime detector leans on both.
    """
    _require(period)
    n = len(candles)
    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    adx_out: Series = [None] * n
    if n < period * 2:
        return plus_di, minus_di, adx_out

    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    trs = true_range(candles)
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    # Wilder sums seeded over bars 1..period (index 0 has no prior bar).
    tr_sum = math.fsum(trs[1: period + 1])
    plus_sum = math.fsum(plus_dm[1: period + 1])
    minus_sum = math.fsum(minus_dm[1: period + 1])

    dx_values: List[Tuple[int, float]] = []

    def emit(idx: int) -> None:
        if tr_sum <= 0:
            return
        p_di = 100.0 * plus_sum / tr_sum
        m_di = 100.0 * minus_sum / tr_sum
        plus_di[idx] = p_di
        minus_di[idx] = m_di
        total = p_di + m_di
        if total > 0:
            dx_values.append((idx, 100.0 * abs(p_di - m_di) / total))

    emit(period)
    for i in range(period + 1, n):
        tr_sum = tr_sum - (tr_sum / period) + trs[i]
        plus_sum = plus_sum - (plus_sum / period) + plus_dm[i]
        minus_sum = minus_sum - (minus_sum / period) + minus_dm[i]
        emit(i)

    if len(dx_values) >= period:
        prev = math.fsum(v for _, v in dx_values[:period]) / period
        adx_out[dx_values[period - 1][0]] = prev
        for idx, dx in dx_values[period:]:
            prev = (prev * (period - 1) + dx) / period
            adx_out[idx] = prev

    return plus_di, minus_di, adx_out


def bollinger(
    values: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Tuple[Series, Series, Series, Series, Series]:
    """Returns ``(upper, middle, lower, bandwidth, percent_b)``.

    ``bandwidth`` is ``(upper-lower)/middle`` — the squeeze detector's input.
    ``percent_b`` is where price sits inside the bands (0 = lower, 1 = upper).
    """
    mid = sma(values, period)
    dev = stdev(values, period)
    n = len(values)
    upper: Series = [None] * n
    lower: Series = [None] * n
    width: Series = [None] * n
    pct_b: Series = [None] * n
    for i in range(n):
        m, d = mid[i], dev[i]
        if m is None or d is None:
            continue
        up = m + num_std * d
        lo = m - num_std * d
        upper[i] = up
        lower[i] = lo
        width[i] = safe_div(up - lo, m, 0.0)
        span = up - lo
        pct_b[i] = 0.5 if span == 0 else (values[i] - lo) / span
    return upper, mid, lower, width, pct_b


def keltner(
    candles: Sequence[Candle],
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> Tuple[Series, Series, Series]:
    """Keltner channels: ``(upper, middle, lower)``.

    Bollinger bands sitting *inside* Keltner channels is the classic
    volatility-squeeze condition.
    """
    closes = _closes(candles)
    mid = ema(closes, ema_period)
    rng = atr(candles, atr_period)
    n = len(candles)
    upper: Series = [None] * n
    lower: Series = [None] * n
    for i in range(n):
        if mid[i] is None or rng[i] is None:
            continue
        upper[i] = mid[i] + multiplier * rng[i]  # type: ignore[operator]
        lower[i] = mid[i] - multiplier * rng[i]  # type: ignore[operator]
    return upper, mid, lower


def donchian(
    candles: Sequence[Candle],
    period: int = 20,
    *, exclude_current: bool = True,
) -> Tuple[Series, Series, Series]:
    """Donchian channel: ``(upper, lower, middle)``.

    ``exclude_current`` omits the forming bar from the window, so "price broke
    the 20-bar high" cannot be satisfied by the bar's own high — that would
    make every new extreme a breakout.
    """
    _require(period)
    n = len(candles)
    upper: Series = [None] * n
    lower: Series = [None] * n
    mid: Series = [None] * n
    offset = 1 if exclude_current else 0
    for i in range(period - 1 + offset, n):
        window = candles[i - period + 1 - offset: i + 1 - offset]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        upper[i] = hi
        lower[i] = lo
        mid[i] = (hi + lo) / 2.0
    return upper, lower, mid


def supertrend(
    candles: Sequence[Candle],
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[Series, List[Optional[int]]]:
    """SuperTrend line and direction (+1 up-trend, -1 down-trend)."""
    rng = atr(candles, period)
    n = len(candles)
    line: Series = [None] * n
    direction: List[Optional[int]] = [None] * n

    final_upper: Optional[float] = None
    final_lower: Optional[float] = None
    trend = 1

    for i in range(n):
        if rng[i] is None:
            continue
        hl2 = (candles[i].high + candles[i].low) / 2.0
        basic_upper = hl2 + multiplier * rng[i]  # type: ignore[operator]
        basic_lower = hl2 - multiplier * rng[i]  # type: ignore[operator]
        prev_close = candles[i - 1].close if i > 0 else candles[i].close

        if final_upper is None or basic_upper < final_upper or prev_close > final_upper:
            final_upper = basic_upper
        if final_lower is None or basic_lower > final_lower or prev_close < final_lower:
            final_lower = basic_lower

        close = candles[i].close
        if close > final_upper:
            trend = 1
        elif close < final_lower:
            trend = -1
        direction[i] = trend
        line[i] = final_lower if trend == 1 else final_upper
    return line, direction


def realized_vol(
    closes: Sequence[float],
    period: int = 30,
    bars_per_year: int = 105_120,  # 5-minute bars
) -> Series:
    """Annualized realized volatility from log returns."""
    _require(period)
    n = len(closes)
    out: Series = [None] * n
    if n < 2:
        return out
    returns: List[Optional[float]] = [None]
    for i in range(1, n):
        prev, cur = closes[i - 1], closes[i]
        returns.append(math.log(cur / prev) if prev > 0 and cur > 0 else 0.0)

    scale = math.sqrt(bars_per_year)
    for i in range(period, n):
        window = [r for r in returns[i - period + 1: i + 1] if r is not None]
        if len(window) < period:
            continue
        mean = math.fsum(window) / len(window)
        var = math.fsum((r - mean) ** 2 for r in window) / max(1, len(window) - 1)
        out[i] = math.sqrt(var) * scale
    return out


def percentile_rank(values: Sequence[Optional[float]], lookback: int) -> Series:
    """Where each value sits within its trailing window, as 0.0-1.0.

    Used to ask "is the current Bollinger bandwidth in the bottom 20% of the
    last N bars?" without hard-coding an absolute width threshold that would
    only be right for one asset.
    """
    _require(lookback)
    n = len(values)
    out: Series = [None] * n
    for i in range(n):
        window = [v for v in values[max(0, i - lookback + 1): i + 1] if v is not None]
        current = values[i]
        if current is None or len(window) < max(2, lookback // 2):
            continue
        below = sum(1 for v in window if v < current)
        equal = sum(1 for v in window if v == current)
        out[i] = (below + 0.5 * equal) / len(window)
    return out


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


def obv(candles: Sequence[Candle]) -> List[float]:
    """On-balance volume — cumulative volume signed by the bar's direction."""
    out: List[float] = [0.0] * len(candles)
    total = 0.0
    for i in range(1, len(candles)):
        if candles[i].close > candles[i - 1].close:
            total += candles[i].volume
        elif candles[i].close < candles[i - 1].close:
            total -= candles[i].volume
        out[i] = total
    return out


def vwap(candles: Sequence[Candle], period: int = 20) -> Series:
    """Rolling volume-weighted average price."""
    _require(period)
    n = len(candles)
    out: Series = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1: i + 1]
        vol = math.fsum(c.volume for c in window)
        if vol <= 0:
            out[i] = math.fsum(c.typical for c in window) / period
        else:
            out[i] = math.fsum(c.typical * c.volume for c in window) / vol
    return out


def volume_ratio(candles: Sequence[Candle], period: int = 20) -> Series:
    """Current bar volume divided by its rolling average (1.0 = average)."""
    volumes = [c.volume for c in candles]
    averages = sma(volumes, period)
    out: Series = [None] * len(candles)
    for i, avg in enumerate(averages):
        if avg is None or avg <= 0:
            continue
        out[i] = volumes[i] / avg
    return out


# --------------------------------------------------------------------------- #
# Equity-curve statistics
# --------------------------------------------------------------------------- #


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.2 = -20%)."""
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


__all__ = [
    "adx",
    "atr",
    "bollinger",
    "donchian",
    "ema",
    "keltner",
    "last_value",
    "linreg_slope",
    "macd",
    "max_drawdown",
    "obv",
    "percentile_rank",
    "realized_vol",
    "roc",
    "rsi",
    "sma",
    "stdev",
    "stochastic",
    "supertrend",
    "true_range",
    "volume_ratio",
    "vwap",
    "wilder_smooth",
    "williams_r",
    "zscore",
]
