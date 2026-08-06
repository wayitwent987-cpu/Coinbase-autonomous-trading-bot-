"""Strategy behaviour and the regime router.

The important assertions are the negative ones: strategies must *not* fire in
regimes they are unsuited to, and the router must not open positions during a
crash.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coinbot.config import StrategyConfig
from coinbot.data.features import FeatureParams, compute_features
from coinbot.models import Position, Regime, RegimeState, SignalAction
from coinbot.regime.detector import RegimeDetector
from coinbot.strategies import (
    BreakoutStrategy,
    MeanReversionStrategy,
    RangeScalpStrategy,
    SqueezeBreakStrategy,
    StrategyContext,
    StrategyRouter,
    TrendFollowStrategy,
)

import synthetic as S

PARAMS = FeatureParams()
ALL_STRATEGIES = [
    TrendFollowStrategy,
    BreakoutStrategy,
    MeanReversionStrategy,
    RangeScalpStrategy,
    SqueezeBreakStrategy,
]


def context(candles, regime=Regime.TRENDING_UP, confidence=0.8, position=None, shorts=False):
    features = compute_features("T-USD", candles, PARAMS)
    return StrategyContext(
        product_id="T-USD",
        features=features,
        regime=RegimeState(regime=regime, confidence=confidence),
        position=position,
        allow_shorts=shorts,
    )


def replay(candles, router, *, shorts=False, start=230, detector=None):
    """Run the router bar by bar; return every non-HOLD decision."""
    detector = detector or RegimeDetector()
    decisions = []
    for i in range(start, len(candles)):
        features = compute_features("T-USD", candles[: i + 1], PARAMS)
        if features is None:
            continue
        state = detector.detect(features)
        ctx = StrategyContext(
            product_id="T-USD", features=features, regime=state, allow_shorts=shorts
        )
        decision = router.evaluate(ctx)
        if decision.action is not SignalAction.HOLD:
            decisions.append((state.regime, decision))
    return decisions


# --------------------------------------------------------------------------- #
# Shared contracts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_strategies_hold_while_warming_up(cls):
    strategy = cls()
    ctx = context(S.trending_up(n=40))
    if ctx.features is None:
        return
    assert strategy.generate(ctx).action is SignalAction.HOLD


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_strategies_reject_unknown_parameters(cls):
    with pytest.raises(ValueError, match="unknown parameter"):
        cls({"definitely_not_a_real_parameter": 1})


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_signals_carry_a_valid_stop(cls):
    """Any entry must have a stop on the correct side of price."""
    strategy = cls()
    for tape, regime in (
        (S.trending_up(), Regime.TRENDING_UP),
        (S.ranging(), Regime.RANGING),
        (S.squeeze(release_bars=8), Regime.SQUEEZE),
    ):
        for i in range(len(tape) - 20, len(tape)):
            ctx = context(tape[: i + 1], regime=regime, shorts=True)
            if ctx.features is None:
                continue
            strategy.observe(ctx)
            signal = strategy.generate(ctx)
            if not signal.is_entry:
                continue
            assert signal.stop_price is not None
            if signal.action is SignalAction.ENTER_LONG:
                assert float(signal.stop_price) < ctx.price
            else:
                assert float(signal.stop_price) > ctx.price
            assert 0.0 <= signal.strength <= 1.0


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_strategies_do_not_short_when_shorts_are_disabled(cls):
    strategy = cls()
    for tape in (S.trending_down(), S.volatile_chop()):
        for i in range(len(tape) - 30, len(tape)):
            ctx = context(tape[: i + 1], regime=Regime.TRENDING_DOWN, shorts=False)
            if ctx.features is None:
                continue
            strategy.observe(ctx)
            assert strategy.generate(ctx).action is not SignalAction.ENTER_SHORT


# --------------------------------------------------------------------------- #
# Individual strategies
# --------------------------------------------------------------------------- #


def test_trend_follow_only_trades_trending_regimes():
    strategy = TrendFollowStrategy()
    ctx = context(S.trending_up(), regime=Regime.RANGING)
    signal = strategy.generate(ctx)
    assert signal.action is SignalAction.HOLD
    assert "not traded" in signal.reason


def test_trend_follow_can_join_a_fresh_trend():
    """A pullback-only trend system never enters a smooth advance."""
    strategy = TrendFollowStrategy()
    fired = False
    tape = S.trending_up(n=400, seed=51)
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.TRENDING_UP)
        if ctx.features is None:
            continue
        if strategy.generate(ctx).action is SignalAction.ENTER_LONG:
            fired = True
            break
    assert fired


def test_breakout_requires_volume_confirmation():
    strategy = BreakoutStrategy({"min_volume_ratio": 99.0, "require_volume": True})
    tape = S.trending_up(n=400, seed=52)
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.TRENDING_UP)
        if ctx.features is None:
            continue
        assert strategy.generate(ctx).action is not SignalAction.ENTER_LONG


def test_mean_reversion_refuses_to_fade_a_strong_trend():
    strategy = MeanReversionStrategy({"max_adx": 5.0})
    tape = S.trending_down(n=400, seed=53)
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.RANGING, shorts=True)
        if ctx.features is None:
            continue
        strategy.observe(ctx)
        assert not strategy.generate(ctx).is_entry


def test_mean_reversion_fires_on_a_stretch_then_turn():
    """The setup and the trigger land on different bars.

    Uses the mean-reverting (OU) tape rather than a sine wave: a clean sine
    has long directional legs and reads as ADX 40+, which this strategy
    deliberately refuses to fade.
    """
    strategy = MeanReversionStrategy()
    tape = S.ranging(n=400, seed=3)
    fired = False
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.RANGING, shorts=True)
        if ctx.features is None:
            continue
        strategy.observe(ctx)
        if strategy.generate(ctx).is_entry:
            fired = True
            break
    assert fired


def test_mean_reversion_will_not_fade_a_clean_directional_swing():
    """A 50-bar sine trends hard within each leg; ADX gates it out."""
    strategy = MeanReversionStrategy()
    tape = S.sine_wave(n=400, seed=54, amplitude=0.06, period=50)
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.RANGING, shorts=True)
        if ctx.features is None:
            continue
        strategy.observe(ctx)
        assert not strategy.generate(ctx).is_entry


def test_range_scalp_needs_a_wide_enough_range():
    strategy = RangeScalpStrategy({"min_range_atr": 999.0})
    tape = S.ranging(n=400, seed=55)
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.RANGING)
        if ctx.features is None:
            continue
        assert not strategy.generate(ctx).is_entry


def test_squeeze_break_tracks_coils_across_regime_changes():
    """observe() must run on every bar, not only in the SQUEEZE regime."""
    strategy = SqueezeBreakStrategy()
    tape = S.squeeze(n=400, seed=56, release_bars=10)
    saw_coil = False
    for i in range(250, len(tape)):
        ctx = context(tape[: i + 1], regime=Regime.TRENDING_UP)
        if ctx.features is None:
            continue
        strategy.observe(ctx)
        if strategy._squeeze_bars.get("T-USD", 0) > 0:
            saw_coil = True
    assert saw_coil, "the coil was never tracked outside the SQUEEZE regime"


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def test_router_rejects_unknown_strategy_names():
    with pytest.raises(ValueError, match="unknown strategy"):
        StrategyRouter(StrategyConfig(enabled=["no_such_strategy"]))


def test_router_routes_by_regime():
    router = StrategyRouter(StrategyConfig())
    assert "trend_follow" in router.active_for(Regime.TRENDING_UP)
    assert "mean_reversion" in router.active_for(Regime.RANGING)
    assert router.active_for(Regime.CRASH) == {}


def test_router_never_enters_during_a_crash():
    router = StrategyRouter(StrategyConfig())
    for tape in (S.crash(n=600, seed=57), S.crash(n=600, seed=58, per_bar=-0.05)):
        for regime, decision in replay(tape, router, shorts=True):
            assert not (regime is Regime.CRASH and decision.is_entry), (
                f"opened a position during a crash: {decision}"
            )


def test_router_produces_entries_in_trending_markets():
    router = StrategyRouter(StrategyConfig())
    decisions = replay(S.trending_up(n=500, seed=59), router)
    assert any(d.action is SignalAction.ENTER_LONG for _r, d in decisions)


def test_router_stays_flat_in_a_downtrend_without_shorts():
    router = StrategyRouter(StrategyConfig())
    decisions = replay(S.trending_down(n=500, seed=60), router, shorts=False)
    assert all(d.action is not SignalAction.ENTER_SHORT for _r, d in decisions)


def test_router_shorts_a_downtrend_when_permitted():
    router = StrategyRouter(StrategyConfig())
    decisions = replay(S.trending_down(n=500, seed=61), router, shorts=True)
    assert any(d.action is SignalAction.ENTER_SHORT for _r, d in decisions)


def test_router_gates_on_signal_strength():
    strict = StrategyRouter(StrategyConfig(min_signal_strength=0.99))
    lenient = StrategyRouter(StrategyConfig(min_signal_strength=0.05))
    tape = S.trending_up(n=500, seed=62)
    assert len(replay(tape, strict)) < len(replay(tape, lenient))


def test_size_multiplier_is_separate_from_strength():
    """Conviction gates entry; regime confidence scales size."""
    router = StrategyRouter(StrategyConfig())
    for _regime, decision in replay(S.trending_up(n=500, seed=63), router):
        assert 0.0 <= decision.strength <= 1.0
        assert 0.0 <= decision.size_multiplier <= 1.0
        assert decision.sized_conviction <= decision.strength + 1e-9


def test_router_exits_win_over_entries():
    router = StrategyRouter(StrategyConfig())
    tape = S.trending_up(n=400, seed=64)
    position = Position(
        product_id="T-USD", size=Decimal("1"),
        entry_price=Decimal("100"), strategy="trend_follow",
    )
    features = compute_features("T-USD", tape, PARAMS)
    ctx = StrategyContext(
        product_id="T-USD", features=features,
        regime=RegimeState(regime=Regime.CRASH, confidence=0.9),
        position=position,
    )
    decision = router.evaluate(ctx)
    # No strategy trades a crash, so the only legal outcome is exit or hold.
    assert decision.action in (SignalAction.EXIT, SignalAction.HOLD)


def test_a_broken_strategy_does_not_stop_the_others(monkeypatch):
    router = StrategyRouter(StrategyConfig())

    def explode(self, ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(TrendFollowStrategy, "evaluate", explode)
    decisions = replay(S.trending_up(n=450, seed=65), router)
    # breakout still gets to speak even though trend_follow raises.
    assert any(d.strategy == "breakout" for _r, d in decisions)
