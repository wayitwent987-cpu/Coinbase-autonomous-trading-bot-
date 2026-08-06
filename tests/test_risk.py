"""Risk manager: sizing, limits, stops and circuit breakers.

These are the tests that matter most. A strategy bug costs one trade; a risk
bug costs the account.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coinbot.config import RiskConfig
from coinbot.data.features import FeatureParams, compute_features
from coinbot.models import Fill, Position, Regime, Side, TradeRecord
from coinbot.portfolio.book import PositionBook
from coinbot.risk.manager import RiskManager

from synthetic import ranging, trending_up

EQUITY = Decimal("10000")


@pytest.fixture
def features():
    return compute_features("BTC-USD", trending_up(n=400, seed=31), FeatureParams())


@pytest.fixture
def risk():
    return RiskManager(RiskConfig())


@pytest.fixture
def book():
    return PositionBook(EQUITY)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def test_risk_per_trade_is_respected(risk, book, product, features):
    price = Decimal("100")
    stop = Decimal("95")  # 5% away
    result = risk.size_position(
        product=product, price=price, direction=1, stop_price=stop,
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book,
        features=features,
    )
    assert result.is_tradeable
    # Risking 0.75% of 10,000 = 75, over a 5-point stop = 15 units.
    assert result.risk_amount == pytest.approx(Decimal("75"), abs=Decimal("0.5"))
    assert result.size == pytest.approx(Decimal("15"), abs=Decimal("0.01"))


def test_conviction_scales_size_linearly(risk, book, product, features):
    def size_for(conviction):
        return risk.size_position(
            product=product, price=Decimal("100"), direction=1, stop_price=Decimal("95"),
            conviction=conviction, equity=EQUITY, available_cash=EQUITY,
            book=book, features=features,
        ).size

    full = size_for(1.0)
    half = size_for(0.5)
    assert half == pytest.approx(full / 2, rel=Decimal("0.01"))


def test_wider_stop_gives_smaller_position(risk, book, product, features):
    """Stops of 5 and 10 points, both loose enough that risk is the binding cap.

    A much tighter stop would be capped by concentration instead and the equal
    risk property would no longer hold — which is correct behaviour, just a
    different constraint doing the work.
    """
    tight = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("95"),
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    wide = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("90"),
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert tight.binding_constraint == wide.binding_constraint == "risk_per_trade"
    assert tight.size > wide.size
    # Both risk the same amount — that is the point of risk-based sizing.
    assert tight.risk_amount == pytest.approx(wide.risk_amount, rel=Decimal("0.05"))


def test_concentration_cap_overrides_a_very_tight_stop(risk, book, product, features):
    """A 1-point stop would demand 75 units; the 25%-of-equity cap wins."""
    result = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("99"),
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert result.binding_constraint == "max_position_pct"
    assert result.notional <= EQUITY * Decimal(str(RiskConfig().max_position_pct))
    # Risking *less* than budget is always acceptable; more never is.
    assert result.risk_amount < EQUITY * Decimal(str(RiskConfig().risk_per_trade_pct))


def test_position_never_exceeds_concentration_cap(risk, book, product, features):
    cfg = RiskConfig(max_position_pct=0.10, risk_per_trade_pct=0.10, use_vol_targeting=False)
    manager = RiskManager(cfg)
    result = manager.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("99.9"),
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert result.notional <= EQUITY * Decimal("0.10")
    assert result.binding_constraint == "max_position_pct"


def test_position_never_exceeds_available_cash(risk, book, product, features):
    result = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("99.5"),
        conviction=1.0, equity=EQUITY, available_cash=Decimal("500"),
        book=book, features=features,
    )
    assert result.notional <= Decimal("500")


def test_sizing_rejects_dust_trades(risk, book, product, features):
    result = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("95"),
        conviction=1.0, equity=Decimal("20"), available_cash=Decimal("20"),
        book=book, features=features,
    )
    assert not result.is_tradeable
    assert "below minimum" in result.rejected


def test_sizing_survives_a_nonsense_stop(risk, book, product, features):
    """A stop on the wrong side of price must not produce an infinite size."""
    result = risk.size_position(
        product=product, price=Decimal("100"), direction=1,
        stop_price=Decimal("105"),  # above entry on a long: invalid
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert result.stop_price < Decimal("100")
    assert result.size > 0
    assert result.notional < EQUITY * Decimal("2")


def test_sizing_rejects_zero_conviction(risk, book, product, features):
    result = risk.size_position(
        product=product, price=Decimal("100"), direction=1, stop_price=Decimal("95"),
        conviction=0.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert not result.is_tradeable


def test_size_is_rounded_to_the_base_increment(risk, book, features):
    from coinbot.models import Product

    coarse = Product(
        product_id="X-USD", base_currency="X", quote_currency="USD",
        base_increment=Decimal("0.1"), quote_increment=Decimal("0.01"),
        price_increment=Decimal("0.01"),
    )
    result = risk.size_position(
        product=coarse, price=Decimal("100"), direction=1, stop_price=Decimal("95"),
        conviction=1.0, equity=EQUITY, available_cash=EQUITY, book=book, features=features,
    )
    assert result.size % Decimal("0.1") == 0


# --------------------------------------------------------------------------- #
# Entry permission
# --------------------------------------------------------------------------- #


def test_no_entries_during_a_crash(risk, book):
    verdict = risk.check_entry("BTC-USD", 1, book, EQUITY, regime=Regime.CRASH)
    assert not verdict
    assert "crash" in verdict.reason.lower()


def test_max_open_positions_enforced(book):
    manager = RiskManager(RiskConfig(max_open_positions=2))
    for pid in ("A-USD", "B-USD"):
        book.apply_fill(
            Fill(order_id="o", product_id=pid, side=Side.BUY,
                 size=Decimal("1"), price=Decimal("10"), fee=Decimal("0"))
        )
    verdict = manager.check_entry("C-USD", 1, book, EQUITY)
    assert not verdict
    assert "max open positions" in verdict.reason


def test_will_not_double_up_on_a_product(risk, book):
    book.apply_fill(
        Fill(order_id="o", product_id="BTC-USD", side=Side.BUY,
             size=Decimal("1"), price=Decimal("100"), fee=Decimal("0"))
    )
    verdict = risk.check_entry("BTC-USD", 1, book, EQUITY)
    assert not verdict
    assert "already holding" in verdict.reason


def test_wide_spread_blocks_entry(risk, book):
    verdict = risk.check_entry("BTC-USD", 1, book, EQUITY, spread_bps=500.0)
    assert not verdict
    assert "spread" in verdict.reason


def test_cooldown_after_a_loss(risk, book):
    losing = TradeRecord(
        product_id="BTC-USD", strategy="s", side=Side.BUY, size=Decimal("1"),
        entry_price=Decimal("100"), exit_price=Decimal("90"), pnl=Decimal("-10"),
        fees=Decimal("0"), opened_at=0, closed_at=1,
    )
    risk.on_trade_closed(losing)
    assert not risk.check_entry("BTC-USD", 1, book, EQUITY)

    for _ in range(RiskConfig().cooldown_bars_after_loss):
        risk.tick_cooldowns()
    assert risk.check_entry("BTC-USD", 1, book, EQUITY)


def test_correlated_positions_are_blocked(book):
    manager = RiskManager(RiskConfig(correlation_cap=0.8))
    tape = trending_up(n=200, seed=41)
    book.apply_fill(
        Fill(order_id="o", product_id="ETH-USD", side=Side.BUY,
             size=Decimal("1"), price=Decimal("100"), fee=Decimal("0"))
    )
    # Identical price history: perfectly correlated, so it is one bet.
    verdict = manager.check_entry(
        "BTC-USD", 1, book, EQUITY,
        candles_by_product={"BTC-USD": tape, "ETH-USD": tape},
    )
    assert not verdict
    assert "correlation" in verdict.reason


def test_uncorrelated_positions_are_allowed(book):
    manager = RiskManager(RiskConfig(correlation_cap=0.8))
    book.apply_fill(
        Fill(order_id="o", product_id="ETH-USD", side=Side.BUY,
             size=Decimal("1"), price=Decimal("100"), fee=Decimal("0"))
    )
    verdict = manager.check_entry(
        "BTC-USD", 1, book, EQUITY,
        candles_by_product={
            "BTC-USD": trending_up(n=200, seed=42),
            "ETH-USD": ranging(n=200, seed=99),
        },
    )
    assert verdict


# --------------------------------------------------------------------------- #
# Circuit breakers
# --------------------------------------------------------------------------- #


def _drawdown_only(max_drawdown_pct: float = 0.10) -> RiskConfig:
    """Isolate the drawdown breaker from the daily/weekly loss limits."""
    return RiskConfig(
        max_drawdown_pct=max_drawdown_pct,
        halt_on_drawdown=True,
        daily_loss_limit_pct=0.99,
        weekly_loss_limit_pct=0.99,
        max_consecutive_losses=999,
    )


def test_drawdown_breaker_halts_trading(book):
    manager = RiskManager(_drawdown_only(0.10))
    assert manager.check_circuit_breakers(book, Decimal("10000"))
    assert manager.check_circuit_breakers(book, Decimal("9500"))   # -5%, fine
    verdict = manager.check_circuit_breakers(book, Decimal("8500"))  # -15%
    assert not verdict
    assert manager.halted
    assert "drawdown" in verdict.reason


def test_drawdown_halt_does_not_self_clear(book):
    manager = RiskManager(_drawdown_only(0.10))
    manager.check_circuit_breakers(book, Decimal("10000"))
    manager.check_circuit_breakers(book, Decimal("8000"))
    assert manager.halted
    # Even after recovery, a drawdown halt needs a human decision.
    assert not manager.check_circuit_breakers(book, Decimal("10500"))
    manager.resume()
    assert manager.check_circuit_breakers(book, Decimal("10500"))


def test_daily_loss_limit_halts(book):
    manager = RiskManager(RiskConfig(daily_loss_limit_pct=0.03, max_drawdown_pct=0.99))
    manager.check_circuit_breakers(book, Decimal("10000"))
    verdict = manager.check_circuit_breakers(book, Decimal("9600"))  # -4%
    assert not verdict
    assert "daily loss" in verdict.reason


def test_consecutive_losses_halt(book):
    manager = RiskManager(RiskConfig(max_consecutive_losses=3, max_drawdown_pct=0.99))
    for i in range(3):
        book.stats.record_trade(
            TradeRecord(
                product_id="BTC-USD", strategy="s", side=Side.BUY, size=Decimal("1"),
                entry_price=Decimal("100"), exit_price=Decimal("99"), pnl=Decimal("-1"),
                fees=Decimal("0"), opened_at=i, closed_at=i + 1,
            )
        )
    verdict = manager.check_circuit_breakers(book, Decimal("10000"))
    assert not verdict
    assert "consecutive losses" in verdict.reason


# --------------------------------------------------------------------------- #
# Stops
# --------------------------------------------------------------------------- #


def test_stop_only_ever_tightens(risk, features):
    position = Position(
        product_id="BTC-USD", size=Decimal("1"),
        entry_price=Decimal(str(features.close)), stop_price=Decimal("1"),
    )
    position.stop_price = Decimal(str(features.close * 0.9))
    before = position.stop_price
    risk.update_stops(position, features)
    assert position.stop_price >= before

    # A later, worse candidate must not loosen the stop.
    tightened = position.stop_price
    position.trail_anchor = Decimal(str(features.close * 0.5))
    risk.update_stops(position, features)
    assert position.stop_price >= tightened


def test_breakeven_stop_after_target_r(features):
    manager = RiskManager(RiskConfig(breakeven_at_r=1.0, use_trailing_stop=False))
    entry = Decimal(str(features.close)) - Decimal("10")
    position = Position(
        product_id="BTC-USD", size=Decimal("1"),
        entry_price=entry, stop_price=entry - Decimal("5"),
    )
    manager.update_stops(position, features)
    # Price is more than 1R above entry, so the stop should be at least breakeven.
    assert position.stop_price >= entry


def test_stop_hit_is_detected_intrabar(risk, features):
    from coinbot.models import Candle

    position = Position(
        product_id="BTC-USD", size=Decimal("1"),
        entry_price=Decimal("100"), stop_price=Decimal("95"),
    )
    # The bar closes above the stop but its low pierced it: that is a stop-out.
    bar = Candle(start=0, open=99, high=101, low=94, close=99, volume=1)
    check = risk.check_exit(position, features, bar=bar)
    assert check.should_exit and check.urgent
    assert "stop" in check.reason


def test_time_stop(risk, features):
    manager = RiskManager(RiskConfig(max_bars_in_trade=10))
    position = Position(
        product_id="BTC-USD", size=Decimal("1"),
        entry_price=Decimal(str(features.close)), bars_held=11,
    )
    check = manager.check_exit(position, features)
    assert check.should_exit
    assert "time stop" in check.reason


def test_hard_stop_backstop_when_no_stop_is_set(features):
    manager = RiskManager(RiskConfig(hard_stop_pct=0.10))
    position = Position(
        product_id="BTC-USD", size=Decimal("1"),
        entry_price=Decimal(str(features.close / 0.8)),  # 20% underwater
    )
    check = manager.check_exit(position, features)
    assert check.should_exit and check.urgent
    assert "hard stop" in check.reason


def test_initial_stop_rejects_wrong_side(risk, features):
    price = Decimal("100")
    stop = risk.initial_stop(price, 1, features, proposed=Decimal("110"))
    assert stop < price

    stop = risk.initial_stop(price, -1, features, proposed=Decimal("90"))
    assert stop > price


def test_initial_stop_caps_absurd_width(features):
    manager = RiskManager(RiskConfig(hard_stop_pct=0.10))
    stop = manager.initial_stop(Decimal("100"), 1, features, proposed=Decimal("10"))
    assert stop >= Decimal("90")
