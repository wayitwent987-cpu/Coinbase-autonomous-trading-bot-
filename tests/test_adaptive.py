"""Adaptive sizing from realized performance.

The properties that matter are the conservative ones: small samples must not
move the needle, a losing combination must never be sized to zero, and winning
streaks must not be allowed to lever up.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coinbot.adaptive import PerformanceTracker
from coinbot.models import Regime, Side, TradeRecord


def trade(strategy="breakout", regime=Regime.TRENDING_UP, return_pct=0.01, fees="0", ts=0.0):
    """A trade whose net return is approximately ``return_pct``."""
    entry = Decimal("100")
    size = Decimal("1")
    exit_price = entry * (Decimal(1) + Decimal(str(return_pct)))
    return TradeRecord(
        product_id="BTC-USD", strategy=strategy, side=Side.BUY, size=size,
        entry_price=entry, exit_price=exit_price,
        pnl=(exit_price - entry) * size, fees=Decimal(fees),
        opened_at=ts, closed_at=ts + 300, regime=regime,
    )


def feed(tracker, n, **kwargs):
    for i in range(n):
        tracker.record_trade(trade(ts=i * 300, **kwargs))


# --------------------------------------------------------------------------- #
# Neutrality and shrinkage
# --------------------------------------------------------------------------- #


def test_no_history_is_neutral():
    tracker = PerformanceTracker()
    assert tracker.multiplier("breakout", "TRENDING_UP") == 1.0


def test_a_couple_of_losses_barely_move_the_size():
    """Two losses is a small sample, not a verdict."""
    tracker = PerformanceTracker(confidence_trades=20)
    feed(tracker, 2, return_pct=-0.01)
    assert 0.9 < tracker.multiplier("breakout", "TRENDING_UP") <= 1.0


def test_a_long_losing_record_shrinks_size_substantially():
    tracker = PerformanceTracker(confidence_trades=20)
    feed(tracker, 100, return_pct=-0.01)
    assert tracker.multiplier("breakout", "TRENDING_UP") < 0.5


def test_more_evidence_moves_the_estimate_further():
    """The same average loss should bite harder with more trades behind it."""
    few, many = PerformanceTracker(), PerformanceTracker()
    feed(few, 5, return_pct=-0.01)
    feed(many, 80, return_pct=-0.01)
    assert many.multiplier("breakout", "TRENDING_UP") < few.multiplier("breakout", "TRENDING_UP")


# --------------------------------------------------------------------------- #
# Bounds — the safety properties
# --------------------------------------------------------------------------- #


def test_never_sizes_to_zero():
    """A banned combination stops producing evidence that conditions changed."""
    tracker = PerformanceTracker(floor=0.25)
    feed(tracker, 500, return_pct=-0.20)
    assert tracker.multiplier("breakout", "TRENDING_UP") == pytest.approx(0.25)


def test_winning_streaks_cannot_lever_up_without_bound():
    tracker = PerformanceTracker(ceiling=1.25)
    feed(tracker, 500, return_pct=0.50)
    assert tracker.multiplier("breakout", "TRENDING_UP") == pytest.approx(1.25)


def test_multiplier_always_within_bounds():
    for pct in (-0.5, -0.05, -0.001, 0.0, 0.001, 0.05, 0.5):
        tracker = PerformanceTracker()
        feed(tracker, 40, return_pct=pct)
        value = tracker.multiplier("breakout", "TRENDING_UP")
        assert tracker.floor <= value <= tracker.ceiling


def test_disabled_tracker_is_always_neutral():
    tracker = PerformanceTracker(enabled=False)
    feed(tracker, 100, return_pct=-0.10)
    assert tracker.multiplier("breakout", "TRENDING_UP") == 1.0


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #


def test_scores_are_per_strategy_and_per_regime():
    """A strategy that fails in chop keeps its size in trends."""
    tracker = PerformanceTracker()
    feed(tracker, 60, strategy="breakout", regime=Regime.VOLATILE_CHOP, return_pct=-0.02)
    feed(tracker, 60, strategy="breakout", regime=Regime.TRENDING_UP, return_pct=0.02)

    assert tracker.multiplier("breakout", "VOLATILE_CHOP") < 0.8
    assert tracker.multiplier("breakout", "TRENDING_UP") > 1.0
    # An untouched combination stays neutral.
    assert tracker.multiplier("mean_reversion", "RANGING") == 1.0


def test_fees_are_included_in_the_score():
    """A gross-positive but fee-negative strategy must score as a loser."""
    tracker = PerformanceTracker()
    for i in range(60):
        # +0.1% gross, but 0.5 in fees on a 100 notional = -0.4% net.
        tracker.record_trade(trade(return_pct=0.001, fees="0.5", ts=i * 300))
    stats = tracker.get("breakout", "TRENDING_UP")
    assert stats.mean_return < 0
    assert tracker.multiplier("breakout", "TRENDING_UP") < 1.0


def test_erratic_results_are_discounted():
    """Same average, wilder swings -> less trusted."""
    steady = PerformanceTracker()
    erratic = PerformanceTracker()
    # Returns kept small so neither case saturates at the ceiling, which
    # would hide the difference the test is looking for.
    for i in range(60):
        steady.record_trade(trade(return_pct=0.001, ts=i * 300))
        erratic.record_trade(trade(return_pct=0.021 if i % 2 else -0.019, ts=i * 300))
    assert erratic.multiplier("breakout", "TRENDING_UP") < steady.multiplier(
        "breakout", "TRENDING_UP"
    )


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


def test_stats_are_recorded():
    tracker = PerformanceTracker()
    feed(tracker, 6, return_pct=0.01)
    feed(tracker, 4, return_pct=-0.01)
    stats = tracker.get("breakout", "TRENDING_UP")
    assert stats.trades == 10
    assert stats.wins == 6
    assert stats.win_rate == pytest.approx(0.6)
    assert stats.profit_factor == pytest.approx(1.5, rel=0.05)


def test_worst_combinations_needs_enough_evidence():
    tracker = PerformanceTracker()
    feed(tracker, 3, strategy="a", return_pct=-0.05)
    feed(tracker, 40, strategy="b", return_pct=-0.05)
    worst = [s.strategy for s in tracker.worst_combinations(minimum_trades=10)]
    assert worst == ["b"]


def test_persistence_round_trip():
    tracker = PerformanceTracker()
    feed(tracker, 30, return_pct=-0.01)
    before = tracker.multiplier("breakout", "TRENDING_UP")

    restored = PerformanceTracker()
    restored.load_json(tracker.to_json())
    assert restored.multiplier("breakout", "TRENDING_UP") == pytest.approx(before)
    assert restored.get("breakout", "TRENDING_UP").trades == 30


def test_corrupt_stored_data_does_not_crash():
    tracker = PerformanceTracker()
    tracker.load_json("this is not json")
    tracker.load_json("")
    tracker.load_json('[{"garbage": 1}]')
    assert tracker.multiplier("breakout", "TRENDING_UP") == 1.0


def test_report_renders():
    tracker = PerformanceTracker()
    assert "no completed trades" in tracker.report()
    feed(tracker, 5, return_pct=0.01)
    assert "breakout/TRENDING_UP" in tracker.report()


def test_rejects_nonsense_bounds():
    with pytest.raises(ValueError):
        PerformanceTracker(floor=1.5, ceiling=1.0)
    with pytest.raises(ValueError):
        PerformanceTracker(confidence_trades=0)
