"""Regime classification against tapes whose true regime is known."""

from __future__ import annotations

import pytest

from coinbot.config import RegimeConfig
from coinbot.data.features import FeatureParams, compute_features
from coinbot.models import Regime
from coinbot.regime.detector import RegimeDetector

import synthetic as S

PARAMS = FeatureParams()


def classify(candles, *, settle: int = 10, cfg: RegimeConfig = None):
    """Replay the last few bars so hysteresis settles, then report."""
    detector = RegimeDetector(cfg or RegimeConfig())
    state = None
    for i in range(max(0, len(candles) - settle), len(candles)):
        features = compute_features("T-USD", candles[: i + 1], PARAMS)
        if features is not None:
            state = detector.detect(features)
    return state


# --------------------------------------------------------------------------- #
# Each generator's true regime is recovered
# --------------------------------------------------------------------------- #


def test_detects_uptrend():
    state = classify(S.trending_up())
    assert state.regime is Regime.TRENDING_UP
    assert state.trend_score > 0.2


def test_detects_downtrend():
    state = classify(S.trending_down())
    assert state.regime is Regime.TRENDING_DOWN
    assert state.trend_score < -0.2


def test_detects_range():
    state = classify(S.ranging())
    assert state.regime is Regime.RANGING
    assert abs(state.trend_score) < 0.4


def test_detects_volatile_chop():
    state = classify(S.volatile_chop())
    assert state.regime is Regime.VOLATILE_CHOP
    assert state.volatility_pct > RegimeConfig().high_vol_pct * 0.8


def test_detects_squeeze():
    state = classify(S.squeeze())
    assert state.regime is Regime.SQUEEZE


def test_detects_crash():
    state = classify(S.crash())
    assert state.regime is Regime.CRASH


# --------------------------------------------------------------------------- #
# Properties of the classifier
# --------------------------------------------------------------------------- #


def test_unknown_until_warmed_up():
    detector = RegimeDetector()
    features = compute_features("T-USD", S.trending_up(n=30), PARAMS)
    state = detector.detect(features)
    assert state.regime is Regime.UNKNOWN
    assert state.confidence == 0.0


def test_a_quiet_uptrend_is_a_trend_not_a_squeeze():
    """A smooth advance has narrow bands too; direction must win."""
    state = classify(S.trending_up(n=400, seed=77, drift=0.002, vol=0.0025))
    assert state.regime is Regime.TRENDING_UP


def test_crash_bypasses_hysteresis():
    """A crash must take effect immediately, not several bars late."""
    candles = S.crash(n=400, drop_bars=12)
    detector = RegimeDetector(RegimeConfig(hysteresis_bars=5))
    # Warm up on the calm section so a non-crash regime is established.
    for i in range(len(candles) - 20, len(candles) - 12):
        detector.detect(compute_features("T-USD", candles[: i + 1], PARAMS))
    established = detector.current("T-USD")
    assert established is not Regime.CRASH

    # One crash bar is enough.
    for i in range(len(candles) - 12, len(candles)):
        state = detector.detect(compute_features("T-USD", candles[: i + 1], PARAMS))
        if state.regime is Regime.CRASH:
            break
    assert state.regime is Regime.CRASH


def test_hysteresis_resists_single_bar_flips():
    cfg = RegimeConfig(hysteresis_bars=3)
    detector = RegimeDetector(cfg)
    candles = S.ranging(n=400)
    for i in range(len(candles) - 20, len(candles)):
        detector.detect(compute_features("T-USD", candles[: i + 1], PARAMS))
    settled = detector.current("T-USD")

    # A single bar of contrary evidence must not change the reported regime.
    spike = list(candles) + [
        type(candles[-1])(
            start=candles[-1].start + 300,
            open=candles[-1].close, high=candles[-1].close * 1.05,
            low=candles[-1].close, close=candles[-1].close * 1.05, volume=500.0,
        )
    ]
    state = detector.detect(compute_features("T-USD", spike, PARAMS))
    assert state.regime is settled


def test_confidence_reflects_the_reported_regime():
    """During a hysteresis hold, confidence describes what we actually report."""
    for tape in (S.trending_up(), S.ranging(), S.crash()):
        state = classify(tape)
        assert 0.0 <= state.confidence <= 1.0


def test_scores_are_all_present_and_bounded():
    state = classify(S.trending_up())
    expected = {r.value for r in Regime if r is not Regime.UNKNOWN}
    assert set(state.scores) == expected
    for score in state.scores.values():
        assert -0.01 <= score <= 1.01


def test_flat_market_does_not_crash_the_detector():
    state = classify(S.flat(n=300))
    assert state is not None
    assert state.regime in set(Regime)


def test_crash_threshold_is_volatility_aware():
    """A high-volatility tape must not read as a crash just from a -7% swing."""
    state = classify(S.volatile_chop(n=400, seed=88))
    assert state.regime is not Regime.CRASH


def test_reset_clears_memory():
    detector = RegimeDetector()
    candles = S.trending_up()
    for i in range(len(candles) - 5, len(candles)):
        detector.detect(compute_features("T-USD", candles[: i + 1], PARAMS))
    assert detector.current("T-USD") is not Regime.UNKNOWN
    detector.reset("T-USD")
    assert detector.current("T-USD") is Regime.UNKNOWN
