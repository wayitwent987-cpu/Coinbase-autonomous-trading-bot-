"""Indicator correctness.

Where a closed-form answer exists it is asserted exactly; otherwise the tests
pin the properties that matter (alignment, bounds, monotonic response) and the
warm-up contract every indicator shares.
"""

from __future__ import annotations

import math

import pytest

from coinbot import indicators as ind
from coinbot.models import Candle

from synthetic import flat, ranging, trending_down, trending_up


def make_candles(closes, highs=None, lows=None):
    out = []
    for i, close in enumerate(closes):
        high = highs[i] if highs else close
        low = lows[i] if lows else close
        out.append(
            Candle(start=i * 300, open=close, high=high, low=low, close=close, volume=100.0)
        )
    return out


# --------------------------------------------------------------------------- #
# Contracts shared by every series indicator
# --------------------------------------------------------------------------- #


def test_series_length_always_matches_input():
    values = list(range(50))
    candles = make_candles([float(v) + 1 for v in values])
    assert len(ind.sma(values, 10)) == len(values)
    assert len(ind.ema(values, 10)) == len(values)
    assert len(ind.rsi([float(v) for v in values], 14)) == len(values)
    assert len(ind.atr(candles, 14)) == len(candles)
    assert len(ind.stdev([float(v) for v in values], 10)) == len(values)


def test_warmup_positions_are_none_not_placeholders():
    values = [float(i) for i in range(20)]
    series = ind.sma(values, 5)
    assert series[:4] == [None] * 4
    assert series[4] is not None


def test_indicators_handle_too_little_data():
    values = [1.0, 2.0]
    assert ind.sma(values, 10) == [None, None]
    assert ind.ema(values, 10) == [None, None]
    assert ind.rsi(values, 14) == [None, None]


def test_invalid_period_rejected():
    with pytest.raises(ValueError):
        ind.sma([1.0, 2.0], 0)


# --------------------------------------------------------------------------- #
# Exact values
# --------------------------------------------------------------------------- #


def test_sma_exact():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ind.sma(values, 3)
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)
    assert result[4] == pytest.approx(4.0)


def test_sma_rolling_sum_matches_naive():
    values = [float((i * 37) % 91) + 0.5 for i in range(200)]
    rolling = ind.sma(values, 14)
    for i in range(13, len(values)):
        naive = sum(values[i - 13: i + 1]) / 14
        assert rolling[i] == pytest.approx(naive, abs=1e-9)


def test_ema_seeded_with_sma_then_recursive():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    result = ind.ema(values, 3)
    assert result[2] == pytest.approx(2.0)  # SMA seed of 1,2,3
    alpha = 2.0 / 4.0
    expected = (4.0 - 2.0) * alpha + 2.0
    assert result[3] == pytest.approx(expected)


def test_ema_reacts_faster_than_sma_to_a_step():
    """Immediately after a jump the EMA has moved further than the SMA.

    Measured two bars in: given enough bars the SMA's window fills with the new
    level and converges exactly, so the comparison only holds while both are
    still responding.
    """
    values = [10.0] * 30 + [20.0, 20.0]
    fast = ind.ema(values, 10)[-1]
    slow = ind.sma(values, 10)[-1]
    assert fast is not None and slow is not None
    assert fast > slow


def test_stdev_exact():
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    result = ind.stdev(values, 8)
    assert result[7] == pytest.approx(2.0)  # population stdev of this classic set


def test_true_range_uses_previous_close():
    candles = [
        Candle(start=0, open=10, high=12, low=9, close=11, volume=1),
        Candle(start=300, open=11, high=20, low=15, close=18, volume=1),
    ]
    trs = ind.true_range(candles)
    assert trs[0] == pytest.approx(3.0)       # first bar: high - low
    assert trs[1] == pytest.approx(9.0)       # |high 20 - prev close 11|


def test_atr_of_constant_range_equals_that_range():
    candles = [
        Candle(start=i * 300, open=100, high=102, low=98, close=100, volume=1)
        for i in range(60)
    ]
    result = ind.atr(candles, 14)
    assert result[-1] == pytest.approx(4.0)


def test_rsi_bounds_and_extremes():
    rising = [float(i) for i in range(1, 60)]
    assert ind.rsi(rising, 14)[-1] == pytest.approx(100.0)

    falling = [float(i) for i in range(60, 1, -1)]
    assert ind.rsi(falling, 14)[-1] == pytest.approx(0.0)

    mixed = ind.rsi([float(50 + (i % 5) - 2) for i in range(80)], 14)
    for value in mixed:
        if value is not None:
            assert 0.0 <= value <= 100.0


def test_rsi_flat_series_is_neutral():
    assert ind.rsi([100.0] * 60, 14)[-1] == pytest.approx(50.0)


def test_bollinger_percent_b_and_bandwidth():
    values = [100.0 + math.sin(i / 3.0) * 5 for i in range(80)]
    upper, mid, lower, width, pct_b = ind.bollinger(values, 20, 2.0)
    i = len(values) - 1
    assert lower[i] < mid[i] < upper[i]
    assert width[i] == pytest.approx((upper[i] - lower[i]) / mid[i])
    assert 0.0 <= pct_b[i] <= 1.0


def test_bollinger_flat_series_has_zero_width():
    _u, _m, _l, width, pct_b = ind.bollinger([50.0] * 40, 20, 2.0)
    assert width[-1] == pytest.approx(0.0)
    assert pct_b[-1] == pytest.approx(0.5)  # degenerate band, defined as centered


def test_donchian_excludes_current_bar():
    closes = [10.0] * 25 + [50.0]
    candles = make_candles(closes, highs=[c for c in closes], lows=[c for c in closes])
    upper, _lower, _mid = ind.donchian(candles, 20, exclude_current=True)
    # The final bar's own high of 50 must not appear in its own channel,
    # otherwise every new extreme would look like a breakout.
    assert upper[-1] == pytest.approx(10.0)

    upper_incl, _l, _m = ind.donchian(candles, 20, exclude_current=False)
    assert upper_incl[-1] == pytest.approx(50.0)


def test_adx_higher_in_trend_than_in_chop():
    trend = trending_up(n=300, seed=21, drift=0.004, vol=0.002)
    chop = ranging(n=300, seed=22)
    _p1, _m1, adx_trend = ind.adx(trend, 14)
    _p2, _m2, adx_chop = ind.adx(chop, 14)
    assert adx_trend[-1] is not None and adx_chop[-1] is not None
    assert adx_trend[-1] > adx_chop[-1]


def test_adx_direction_indicators_agree_with_trend():
    up = trending_up(n=300, seed=23, drift=0.004, vol=0.002)
    plus_di, minus_di, _adx = ind.adx(up, 14)
    assert plus_di[-1] > minus_di[-1]

    down = trending_down(n=300, seed=24, drift=-0.004, vol=0.002)
    plus_di, minus_di, _adx = ind.adx(down, 14)
    assert minus_di[-1] > plus_di[-1]


def test_macd_histogram_is_line_minus_signal():
    values = [100.0 + i * 0.3 + math.sin(i / 5.0) for i in range(120)]
    line, signal, hist = ind.macd(values, 12, 26, 9)
    i = len(values) - 1
    assert hist[i] == pytest.approx(line[i] - signal[i])


def test_macd_rejects_fast_ge_slow():
    with pytest.raises(ValueError):
        ind.macd([1.0] * 50, 26, 12, 9)


def test_percentile_rank_is_tie_aware_and_bounded():
    values = [float(i) for i in range(100)]
    ranks = ind.percentile_rank(values, 50)
    # The newest value is the window maximum, but a tie-aware rank credits it
    # half its own occurrence rather than ranking it above itself: (49+0.5)/50.
    assert ranks[-1] == pytest.approx(0.99)
    for rank in ranks:
        if rank is not None:
            assert 0.0 <= rank <= 1.0


def test_percentile_rank_of_a_low_value_is_low():
    values = [float(i) for i in range(60)] + [-100.0]
    ranks = ind.percentile_rank(values, 40)
    assert ranks[-1] < 0.05


def test_stochastic_bounds():
    candles = ranging(n=200, seed=25)
    k, d = ind.stochastic(candles, 14, 3)
    for series in (k, d):
        for value in series:
            if value is not None:
                assert -0.01 <= value <= 100.01


def test_max_drawdown():
    assert ind.max_drawdown([100, 120, 60, 80]) == pytest.approx(0.5)
    assert ind.max_drawdown([100, 110, 120]) == pytest.approx(0.0)


def test_realized_vol_scales_with_noise():
    calm = trending_up(n=300, seed=26, drift=0.0, vol=0.002)
    wild = trending_up(n=300, seed=26, drift=0.0, vol=0.02)
    calm_vol = ind.realized_vol([c.close for c in calm], 30, 105_120)[-1]
    wild_vol = ind.realized_vol([c.close for c in wild], 30, 105_120)[-1]
    assert wild_vol > calm_vol


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #


def test_flat_market_does_not_divide_by_zero():
    candles = flat(n=100)
    closes = [c.close for c in candles]
    assert ind.atr(candles, 14)[-1] == pytest.approx(0.0)
    assert ind.rsi(closes, 14)[-1] == pytest.approx(50.0)
    _u, _m, _l, width, pct_b = ind.bollinger(closes, 20, 2.0)
    assert width[-1] == pytest.approx(0.0)
    assert pct_b[-1] == pytest.approx(0.5)
    k, _d = ind.stochastic(candles, 14, 3)
    assert k[-1] == pytest.approx(50.0)
    # ADX on a flat tape has no directional movement to measure.
    _p, _m2, adx = ind.adx(candles, 14)
    assert adx[-1] is None or adx[-1] == pytest.approx(0.0)


def test_empty_input():
    assert ind.sma([], 5) == []
    assert ind.atr([], 14) == []
    assert ind.true_range([]) == []
