"""Position book accounting and numeric rounding.

Money math must be exact. These tests pin the invariants that a silent
Decimal/float slip would break.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import pytest

from coinbot.models import Fill, Regime, Side
from coinbot.portfolio.book import PositionBook
from coinbot.util.numeric import (
    quantize_to_increment,
    round_price,
    round_size,
    safe_div,
    to_decimal,
)


def buy(book, product="BTC-USD", size="1", price="100", fee="0", **kwargs):
    return book.apply_fill(
        Fill(order_id="o", product_id=product, side=Side.BUY,
             size=Decimal(size), price=Decimal(price), fee=Decimal(fee)),
        **kwargs,
    )


def sell(book, product="BTC-USD", size="1", price="100", fee="0", **kwargs):
    return book.apply_fill(
        Fill(order_id="o", product_id=product, side=Side.SELL,
             size=Decimal(size), price=Decimal(price), fee=Decimal(fee)),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #


def test_quantize_to_increment():
    assert quantize_to_increment("1.23456", "0.001") == Decimal("1.234")
    assert quantize_to_increment("1.23456", "0.01") == Decimal("1.23")
    assert quantize_to_increment("7", "5", ROUND_DOWN) == Decimal("5")
    assert quantize_to_increment("7", "5", ROUND_CEILING) == Decimal("10")


def test_zero_increment_is_a_no_op():
    assert quantize_to_increment("1.23456", "0") == Decimal("1.23456")


def test_size_always_rounds_down():
    """Rounding a size up could try to sell more than the account holds."""
    assert round_size("0.999999999", "0.00000001") == Decimal("0.99999999")
    assert round_size("1.9", "1") == Decimal("1")


def test_limit_prices_round_conservatively():
    # A buy never rounds up (never pays more than intended).
    assert round_price("100.567", "0.01", side_is_buy=True) == Decimal("100.56")
    # A sell never rounds down (never receives less).
    assert round_price("100.561", "0.01", side_is_buy=False) == Decimal("100.57")


def test_to_decimal_avoids_float_noise():
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


def test_safe_div_handles_zero_and_nan():
    assert safe_div(1.0, 0.0, default=-1.0) == -1.0
    assert safe_div(float("nan"), 1.0, default=0.0) == 0.0
    assert safe_div(4.0, 2.0) == 2.0


# --------------------------------------------------------------------------- #
# Fills
# --------------------------------------------------------------------------- #


def test_open_sets_entry_and_metadata():
    book = PositionBook(Decimal("10000"))
    buy(book, size="2", price="50", strategy="trend_follow", regime=Regime.TRENDING_UP)
    position = book.get("BTC-USD")
    assert position.size == Decimal("2")
    assert position.entry_price == Decimal("50")
    assert position.strategy == "trend_follow"
    assert position.regime_at_entry is Regime.TRENDING_UP


def test_adding_averages_the_entry_price():
    book = PositionBook(Decimal("10000"))
    buy(book, size="10", price="50")
    buy(book, size="10", price="60")
    position = book.get("BTC-USD")
    assert position.size == Decimal("20")
    assert position.entry_price == Decimal("55")


def test_partial_close_keeps_entry_price():
    book = PositionBook(Decimal("10000"))
    buy(book, size="10", price="50")
    trade = sell(book, size="4", price="60")
    assert trade is not None
    assert trade.pnl == Decimal("40")     # 4 units x 10 gain
    assert book.get("BTC-USD").size == Decimal("6")
    assert book.get("BTC-USD").entry_price == Decimal("50")


def test_full_close_flattens_and_records():
    book = PositionBook(Decimal("10000"))
    buy(book, size="5", price="100", strategy="breakout")
    trade = sell(book, size="5", price="110", exit_reason="target")
    assert trade.pnl == Decimal("50")
    assert trade.strategy == "breakout"
    assert trade.exit_reason == "target"
    assert book.get("BTC-USD") is None
    assert book.open_count == 0


def test_flip_through_zero_opens_the_other_side():
    book = PositionBook(Decimal("10000"))
    buy(book, size="10", price="50")
    trade = sell(book, size="15", price="60")
    assert trade.pnl == Decimal("100")           # closed 10 units at +10
    position = book.positions["BTC-USD"]
    assert position.size == Decimal("-5")        # 5 short remain
    assert position.entry_price == Decimal("60")  # re-based at the flip price


def test_cash_and_equity_track_fills():
    book = PositionBook(Decimal("10000"))
    buy(book, size="2", price="100", fee="1")
    assert book.cash == Decimal("9799")           # 10000 - 200 - 1
    book.mark("BTC-USD", Decimal("110"))
    assert book.equity() == Decimal("10019")      # 9799 + 220
    sell(book, size="2", price="110", fee="1")
    assert book.cash == Decimal("10018")
    assert book.equity() == Decimal("10018")


def test_fees_are_counted_once_not_twice():
    """Regression: fees were accumulated per fill *and* per trade record."""
    book = PositionBook(Decimal("10000"))
    buy(book, size="2", price="100", fee="0.5")
    sell(book, size="2", price="110", fee="0.5")
    assert book.stats.fees_paid == Decimal("1.0")
    assert book.stats.realized_pnl == Decimal("20")


def test_trade_record_includes_both_sides_fees():
    """A round trip pays entry and exit fees; reporting one halves the cost."""
    book = PositionBook(Decimal("10000"))
    buy(book, size="2", price="100", fee="0.6")
    trade = sell(book, size="2", price="110", fee="0.4")
    assert trade.fees == Decimal("1.0")
    assert trade.pnl == Decimal("20")        # gross
    assert trade.net_pnl == Decimal("19.0")  # after both fees


def test_partial_close_apportions_fees():
    book = PositionBook(Decimal("10000"))
    buy(book, size="10", price="100", fee="1.0")
    trade = sell(book, size="5", price="110", fee="0.5")
    # Half the position closed: half the accumulated fees go with it.
    assert trade.fees == Decimal("0.75")
    assert book.get("BTC-USD").fees_paid == Decimal("0.75")


def test_stats_track_wins_losses_and_streaks():
    book = PositionBook(Decimal("10000"))
    for price, size in (("110", "1"), ("90", "1"), ("80", "1")):
        buy(book, size=size, price="100")
        sell(book, size=size, price=price)
    assert book.stats.wins == 1
    assert book.stats.losses == 2
    assert book.stats.consecutive_losses == 2
    assert book.stats.win_rate == pytest.approx(1 / 3)


def test_profit_factor():
    book = PositionBook(Decimal("10000"))
    buy(book, size="1", price="100")
    sell(book, size="1", price="120")   # +20
    buy(book, size="1", price="100")
    sell(book, size="1", price="90")    # -10
    assert book.stats.profit_factor == pytest.approx(2.0)


def test_drawdown_tracking():
    book = PositionBook(Decimal("10000"))
    book.stats.mark_equity(Decimal("10000"))
    book.stats.mark_equity(Decimal("12000"))
    book.stats.mark_equity(Decimal("9000"))
    assert book.stats.peak_equity == Decimal("12000")
    assert book.stats.max_drawdown == pytest.approx(0.25)
    assert book.stats.drawdown_from_peak(Decimal("6000")) == pytest.approx(0.5)


def test_unrealized_pnl_for_long_and_short():
    book = PositionBook(Decimal("10000"))
    buy(book, size="2", price="100")
    book.mark("BTC-USD", Decimal("105"))
    assert book.get("BTC-USD").unrealized_pnl == Decimal("10")

    other = PositionBook(Decimal("10000"))
    sell(other, product="ETH-USD", size="2", price="100")
    other.mark("ETH-USD", Decimal("95"))
    assert other.get("ETH-USD").unrealized_pnl == Decimal("10")


def test_gross_exposure_uses_absolute_value():
    book = PositionBook(Decimal("10000"))
    buy(book, product="A-USD", size="1", price="100")
    sell(book, product="B-USD", size="1", price="100")
    book.mark("A-USD", Decimal("100"))
    book.mark("B-USD", Decimal("100"))
    assert book.gross_exposure == Decimal("200")
    assert book.positions_value == Decimal("0")  # long and short net out


def test_bars_held_advances():
    book = PositionBook(Decimal("10000"))
    buy(book, size="1", price="100")
    book.tick_bars()
    book.tick_bars()
    assert book.get("BTC-USD").bars_held == 2
