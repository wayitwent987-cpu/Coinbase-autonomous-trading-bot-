"""Paper broker fill semantics and the execution engine.

These tests defend the assumptions that make paper and backtest results
trustworthy. A simulator that fills too generously or trades for free will
happily "prove" a strategy that loses money the moment it meets a real
exchange.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coinbot.config import ExecutionConfig, RiskConfig
from coinbot.execution.engine import ExecutionEngine
from coinbot.execution.paper import PaperBroker
from coinbot.models import (
    Candle,
    OrderRequest,
    OrderStatus,
    OrderType,
    Quote,
    Side,
)

PRICE = Decimal("100")


@pytest.fixture
def exec_cfg():
    return ExecutionConfig(
        taker_fee_bps=60.0, maker_fee_bps=40.0, slippage_bps=5.0,
        order_type="limit", limit_offset_bps=2.0,
    )


@pytest.fixture
def broker(exec_cfg):
    paper = PaperBroker(starting_cash=Decimal("10000"), exec_cfg=exec_cfg)
    paper.set_mark("BTC-USD", PRICE)
    return paper


@pytest.fixture
def engine(broker, exec_cfg):
    return ExecutionEngine(broker, exec_cfg, RiskConfig())


@pytest.fixture
def quote():
    return Quote(product_id="BTC-USD", bid=Decimal("99.95"), ask=Decimal("100.05"))


def bar(low, high, close=None, start=0):
    return Candle(
        start=start, open=float(PRICE), high=float(high),
        low=float(low), close=float(close if close is not None else PRICE), volume=100.0,
    )


# --------------------------------------------------------------------------- #
# Fill semantics
# --------------------------------------------------------------------------- #


def test_market_order_fills_immediately_with_slippage_against_us(broker, exec_cfg):
    product = broker.get_product("BTC-USD")
    request = OrderRequest(
        product_id="BTC-USD", side=Side.BUY, order_type=OrderType.MARKET,
        base_size=Decimal("1"),
    )
    order = broker.place_order(request)
    assert order.status is OrderStatus.FILLED
    # A buy pays more than the mark, never less.
    assert order.avg_fill_price > PRICE
    assert order.avg_fill_price == PRICE * Decimal("1.0005")


def test_market_sell_slips_downward(broker):
    broker.balances["BTC"] = Decimal("5")
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.SELL,
                     order_type=OrderType.MARKET, base_size=Decimal("1"))
    )
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price < PRICE


def test_touching_a_limit_is_not_a_fill(broker):
    """At the touch there is a queue ahead of us; assuming a fill is fantasy."""
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("99"))
    )
    assert order.status is OrderStatus.OPEN
    broker.on_bar("BTC-USD", bar(low=99, high=101))       # exactly touches
    assert broker.get_order(order.order_id).status is OrderStatus.OPEN


def test_trading_through_a_limit_fills_it(broker):
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("99"))
    )
    broker.on_bar("BTC-USD", bar(low=98.5, high=101))     # trades through
    filled = broker.get_order(order.order_id)
    assert filled.status is OrderStatus.FILLED
    assert filled.avg_fill_price == Decimal("99")


def test_sell_limit_fills_only_when_price_exceeds_it(broker):
    broker.balances["BTC"] = Decimal("5")
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.SELL, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("101"))
    )
    broker.on_bar("BTC-USD", bar(low=99, high=101))       # touch only
    assert broker.get_order(order.order_id).status is OrderStatus.OPEN
    broker.on_bar("BTC-USD", bar(low=99, high=101.5, start=300))
    assert broker.get_order(order.order_id).status is OrderStatus.FILLED


def test_marketable_limit_fills_at_placement(broker):
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("105"))
    )
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price <= Decimal("105")


def test_post_only_order_that_would_cross_is_rejected(broker):
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("105"), post_only=True)
    )
    assert order.status is OrderStatus.CANCELLED
    assert "post-only" in order.reject_reason


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #


def test_taker_and_maker_fees_differ(broker, exec_cfg):
    taker = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY,
                     order_type=OrderType.MARKET, base_size=Decimal("1"))
    )
    resting = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY, order_type=OrderType.LIMIT,
                     base_size=Decimal("1"), limit_price=Decimal("99"))
    )
    broker.on_bar("BTC-USD", bar(low=98, high=101))
    maker = broker.get_order(resting.order_id)

    assert taker.fee > 0 and maker.fee > 0
    # Same notional ballpark, but the passive fill is cheaper.
    assert (maker.fee / maker.filled_size) < (taker.fee / taker.filled_size)


def test_every_fill_is_charged_a_fee(broker):
    broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY,
                     order_type=OrderType.MARKET, base_size=Decimal("1"))
    )
    assert all(f.fee > 0 for f in broker.get_fills())


def test_cash_cannot_go_negative(broker):
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY,
                     order_type=OrderType.MARKET, base_size=Decimal("1000"))
    )
    assert order.status is OrderStatus.FAILED
    assert "insufficient" in order.reject_reason
    assert broker.balances["USD"] == Decimal("10000")


def test_cannot_sell_what_is_not_held(broker):
    order = broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.SELL,
                     order_type=OrderType.MARKET, base_size=Decimal("1"))
    )
    assert order.status is OrderStatus.FAILED
    assert "insufficient" in order.reject_reason


def test_broker_refuses_to_trade_without_a_price(exec_cfg):
    blind = PaperBroker(starting_cash=Decimal("1000"), exec_cfg=exec_cfg)
    with pytest.raises(ValueError, match="no price"):
        blind.place_order(
            OrderRequest(product_id="BTC-USD", side=Side.BUY,
                         order_type=OrderType.MARKET, base_size=Decimal("1"))
        )


# --------------------------------------------------------------------------- #
# Execution engine
# --------------------------------------------------------------------------- #


def test_entry_defaults_to_a_passive_limit(engine, broker, quote):
    product = broker.get_product("BTC-USD")
    result = engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=quote,
        reference_price=PRICE, strategy="trend_follow", reason="test",
    )
    assert result.ok
    assert result.submitted.order_type is OrderType.LIMIT
    # Joins the bid rather than crossing to the ask.
    assert result.submitted.limit_price < quote.ask


def test_urgent_exit_crosses_the_spread(engine, broker, quote):
    broker.balances["BTC"] = Decimal("5")
    product = broker.get_product("BTC-USD")
    result = engine.close_position(
        product=product, side=Side.SELL, size=Decimal("1"), quote=quote,
        reference_price=PRICE, reason="stop hit", urgent=True,
    )
    assert result.submitted.order_type is OrderType.MARKET
    assert result.fills


def test_entry_blocked_by_a_blown_out_spread(engine, broker):
    product = broker.get_product("BTC-USD")
    wide = Quote(product_id="BTC-USD", bid=Decimal("95"), ask=Decimal("105"))
    result = engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=wide,
        reference_price=PRICE, reason="test",
    )
    assert result.submitted is None
    assert "spread" in result.skipped


def test_zero_size_is_skipped(engine, broker, quote):
    product = broker.get_product("BTC-USD")
    result = engine.open_position(
        product=product, side=Side.BUY, size=Decimal("0"), quote=quote,
        reference_price=PRICE, reason="test",
    )
    assert "zero" in result.skipped


def test_resting_order_is_tracked_as_working(engine, broker, quote):
    product = broker.get_product("BTC-USD")
    result = engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=quote,
        reference_price=PRICE, strategy="breakout", reason="test",
    )
    assert result.resting
    assert engine.has_working("BTC-USD")
    assert engine.working_count() == 1


def test_fill_event_carries_intent_through_a_resting_order(engine, broker, quote):
    """Regression: a limit entry that rests must not lose its strategy or stop.

    Before this was fixed, any entry that did not fill instantly came back
    unattributed and with no stop attached.
    """
    product = broker.get_product("BTC-USD")
    stop, target = Decimal("95"), Decimal("110")
    result = engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=quote,
        reference_price=PRICE, strategy="breakout", reason="donchian break",
        stop_price=stop, take_profit=target, regime="TRENDING_UP",
    )
    assert result.resting

    broker.on_bar("BTC-USD", bar(low=98, high=101))
    events = engine.reconcile({"BTC-USD": quote})

    assert len(events) == 1
    event = events[0]
    assert event.is_entry
    assert event.strategy == "breakout"
    assert event.stop_price == stop
    assert event.take_profit == target
    assert event.regime == "TRENDING_UP"
    assert not engine.has_working("BTC-USD")


def test_stale_limit_is_repriced_then_escalated(broker, quote):
    """Patience is bounded: a limit that will not fill becomes a market order."""
    cfg = ExecutionConfig(
        order_type="limit", chase_limit_bars=1, max_order_retries=1,
        cancel_stale_after_s=1e9, taker_fee_bps=60, maker_fee_bps=40,
    )
    engine = ExecutionEngine(broker, cfg, RiskConfig())
    product = broker.get_product("BTC-USD")

    engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=quote,
        reference_price=PRICE, strategy="s", reason="entry",
    )
    assert engine.working_count() == 1

    # Each reconcile with no fill ages the order; eventually it crosses.
    for _ in range(6):
        engine.reconcile({"BTC-USD": quote})
        if not engine.working_count():
            break

    assert any(o.order_type is OrderType.MARKET for o in broker._orders.values()), (
        "the engine never escalated to a market order"
    )


def test_cancel_all_clears_working_orders(engine, broker, quote):
    product = broker.get_product("BTC-USD")
    engine.open_position(
        product=product, side=Side.BUY, size=Decimal("1"), quote=quote,
        reference_price=PRICE, reason="test",
    )
    assert engine.working_count() == 1
    assert engine.cancel_all() == 1
    assert engine.working_count() == 0


def test_account_snapshot_values_holdings(broker):
    broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY,
                     order_type=OrderType.MARKET, base_size=Decimal("2"))
    )
    account = broker.get_account("USD")
    assert account.positions_value > 0
    # Equity should be roughly unchanged by the trade itself, minus fees.
    assert Decimal("9980") < account.equity <= Decimal("10000")


def test_reset_restores_the_starting_state(broker):
    broker.place_order(
        OrderRequest(product_id="BTC-USD", side=Side.BUY,
                     order_type=OrderType.MARKET, base_size=Decimal("1"))
    )
    broker.reset()
    assert broker.balances == {"USD": Decimal("10000")}
    assert broker.get_fills() == []
    assert broker.list_open_orders() == []
