"""Decimal rounding against exchange increments.

Every size and price that leaves the process has to sit exactly on the
product's increment grid or Coinbase rejects the order. Rounding direction
matters: sizes round *down* so we never try to sell more than we hold, and
buy prices round *down* / sell prices round *up* so a limit never becomes
more aggressive than intended.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Union

Number = Union[int, float, str, Decimal]


def to_decimal(value: Number) -> Decimal:
    """Convert to Decimal without inheriting binary-float noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def quantize_to_increment(
    value: Number,
    increment: Number,
    rounding: str = ROUND_DOWN,
) -> Decimal:
    """Snap ``value`` onto the ``increment`` grid.

    ``quantize_to_increment("1.23456", "0.001")`` -> ``Decimal('1.234')``.
    An increment of zero or less means "no constraint" and returns the value.
    """
    value_d = to_decimal(value)
    inc = to_decimal(increment)
    if inc <= 0:
        return value_d
    steps = (value_d / inc).to_integral_value(rounding=rounding)
    result = steps * inc
    # Re-quantize so the exponent matches the increment (0.1 not 0.1000000).
    return result.quantize(inc.normalize(), rounding=ROUND_HALF_UP) if inc < 1 else result


def round_size(size: Number, increment: Number) -> Decimal:
    """Round an order size down to the base increment."""
    return quantize_to_increment(size, increment, ROUND_DOWN)


def round_price(price: Number, increment: Number, side_is_buy: bool) -> Decimal:
    """Round a limit price conservatively for the given side.

    Buys round down (pay no more than intended), sells round up (receive no
    less). Both stay passive relative to the raw target.
    """
    rounding = ROUND_FLOOR if side_is_buy else ROUND_CEILING
    return quantize_to_increment(price, increment, rounding)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp_decimal(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that yields ``default`` instead of raising or returning inf."""
    if denominator == 0 or denominator != denominator:  # NaN check
        return default
    result = numerator / denominator
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def pct_change(new: float, old: float) -> float:
    return safe_div(new - old, abs(old), 0.0)


def bps(fraction: float) -> float:
    """0.0025 -> 25.0 basis points."""
    return fraction * 10_000.0


def from_bps(basis_points: float) -> float:
    """25.0 -> 0.0025."""
    return basis_points / 10_000.0


def fmt_money(value: Number, currency: str = "USD", places: int = 2) -> str:
    quant = Decimal(1).scaleb(-places)
    return f"{to_decimal(value).quantize(quant, ROUND_HALF_UP)} {currency}"


__all__ = [
    "Number",
    "bps",
    "clamp",
    "clamp_decimal",
    "fmt_money",
    "from_bps",
    "pct_change",
    "quantize_to_increment",
    "round_price",
    "round_size",
    "safe_div",
    "to_decimal",
]
