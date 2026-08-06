"""Synthetic price tapes with known regimes, shared by the tests.

Each generator produces a tape whose *true* regime is known by construction,
which is what lets the regime tests assert on classification instead of just
"it returned something".
"""

from __future__ import annotations

import math
import random
from typing import List

from coinbot.models import Candle

BAR_SECONDS = 300


def _build(prices: List[float], vol: float, seed: int, start_ts: int = 0) -> List[Candle]:
    """Wrap a close-price path into OHLCV candles with plausible wicks.

    Volume is generated *proportional to each bar's move* rather than as
    independent noise. Real markets expand volume on decisive bars, and
    several strategies filter on exactly that — with uncorrelated volume a
    breakout's volume confirmation can never be satisfied, so the tape would
    silently test nothing.
    """
    rng = random.Random(seed + 1)
    moves = [
        abs(prices[i] - prices[i - 1]) / max(1e-9, prices[i - 1]) if i else 0.0
        for i in range(len(prices))
    ]
    typical = sorted(moves)[len(moves) // 2] or max(1e-9, vol)

    candles: List[Candle] = []
    for i, close in enumerate(prices):
        open_ = prices[i - 1] if i else close
        wick = max(abs(close - open_) * 0.35, close * vol * 0.4)
        high = max(open_, close) + abs(rng.gauss(0, wick))
        low = min(open_, close) - abs(rng.gauss(0, wick))
        surge = min(4.0, moves[i] / typical)
        candles.append(
            Candle(
                start=start_ts + i * BAR_SECONDS,
                open=open_,
                high=high,
                low=max(0.01, low),
                close=close,
                volume=100.0 * (0.6 + 0.7 * surge) * rng.uniform(0.85, 1.15),
            )
        )
    return candles


def trending_up(n: int = 400, seed: int = 1, drift: float = 0.0020, vol: float = 0.0035) -> List[Candle]:
    """A sustained uptrend: drift clearly dominates per-bar noise."""
    rng = random.Random(seed)
    price = 100.0
    prices = []
    for _ in range(n):
        price *= 1.0 + rng.gauss(drift, vol)
        prices.append(price)
    return _build(prices, vol, seed)


def trending_down(n: int = 400, seed: int = 2, drift: float = -0.0020, vol: float = 0.0035) -> List[Candle]:
    """A sustained downtrend — the mirror of :func:`trending_up`."""
    return trending_up(n, seed, drift=drift, vol=vol)


def ranging(n: int = 400, seed: int = 3, center: float = 100.0, sigma: float = 0.006) -> List[Candle]:
    """Mean-reverting (Ornstein-Uhlenbeck) chop inside a tight band."""
    rng = random.Random(seed)
    theta = 0.16
    price = center
    prices = []
    for _ in range(n):
        price += theta * (center - price) + rng.gauss(0.0, center * sigma)
        prices.append(max(1.0, price))
    return _build(prices, sigma, seed)


def volatile_chop(n: int = 400, seed: int = 4, vol: float = 0.030) -> List[Candle]:
    """Wide, directionless swings: high volatility, no net drift."""
    rng = random.Random(seed)
    center = 100.0
    price = center
    prices = []
    for _ in range(n):
        # A firm pull to the center stops the random walk from accumulating
        # into a trend, which is what separates "chop" from "volatile trend".
        pull = 0.30 * (center - price) / center
        price *= 1.0 + rng.gauss(pull, vol)
        prices.append(max(1.0, price))
    return _build(prices, vol, seed)


def squeeze(n: int = 400, seed: int = 5, release_bars: int = 0) -> List[Candle]:
    """Volatility contracting to a fraction of its earlier level.

    With ``release_bars > 0`` the coil resolves into a sharp upward expansion
    over the final bars, which is what actually exercises a squeeze-breakout
    trigger — a coil that never releases only tests the waiting half.
    """
    rng = random.Random(seed)
    coil_end = n - release_bars
    price = 100.0
    prices = []
    for i in range(n):
        if i >= coil_end:
            shock = rng.gauss(0.018, 0.004)  # the expansion
        else:
            progress = max(0.0, (i - coil_end * 0.66) / (coil_end * 0.34))
            vol = 0.012 * (1.0 - 0.90 * min(1.0, progress))
            shock = rng.gauss(0.0, vol)
        price *= 1.0 + shock
        prices.append(max(1.0, price))
    return _build(prices, 0.002, seed)


def crash(n: int = 400, seed: int = 6, drop_bars: int = 12, per_bar: float = -0.030) -> List[Candle]:
    """A quiet tape that falls off a cliff in the final ``drop_bars``."""
    rng = random.Random(seed)
    price = 100.0
    prices = []
    for i in range(n):
        if i >= n - drop_bars:
            shock = per_bar + rng.gauss(0.0, 0.004)
        else:
            shock = rng.gauss(0.0002, 0.007)
        price *= 1.0 + shock
        prices.append(max(0.5, price))
    return _build(prices, 0.007, seed)


def sine_wave(n: int = 400, seed: int = 7, amplitude: float = 0.05, period: int = 40) -> List[Candle]:
    """A clean oscillation — the ideal tape for mean-reversion entries."""
    rng = random.Random(seed)
    prices = [
        100.0 * (1.0 + amplitude * math.sin(2 * math.pi * i / period)) + rng.gauss(0, 0.05)
        for i in range(n)
    ]
    return _build(prices, 0.004, seed)


def flat(n: int = 400, price: float = 100.0) -> List[Candle]:
    """A perfectly flat tape — exercises divide-by-zero paths in indicators."""
    return [
        Candle(start=i * BAR_SECONDS, open=price, high=price, low=price, close=price, volume=1.0)
        for i in range(n)
    ]


GENERATORS = {
    "trending_up": trending_up,
    "trending_down": trending_down,
    "ranging": ranging,
    "volatile_chop": volatile_chop,
    "squeeze": squeeze,
    "crash": crash,
    "sine_wave": sine_wave,
}

__all__ = [
    "BAR_SECONDS",
    "GENERATORS",
    "crash",
    "flat",
    "ranging",
    "sine_wave",
    "squeeze",
    "trending_down",
    "trending_up",
    "volatile_chop",
]
