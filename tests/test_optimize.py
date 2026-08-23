"""Tests for the walk-forward optimizer and its honesty checks.

The distinction these tests exist to protect is the one the whole package is
for: a number computed on data the search saw, versus one computed on data it
did not. Most of the assertions here are about the *plumbing* that keeps those
two apart — that a test window never overlaps its training window, that warm-up
bars come from before a window rather than out of it, and that the parallel and
serial paths produce identical results.

Backtests are slow, so the end-to-end tests use short tapes and tiny spaces.
They check that the machinery is wired correctly, not that any strategy makes
money on synthetic data.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from coinbot.backtest.metrics import Metrics
from coinbot.config import Config
from coinbot.models import Side, TradeRecord
from coinbot.optimize.objective import OBJECTIVES, get_objective
from coinbot.optimize.robustness import (
    deflated_sharpe,
    monte_carlo,
    returns_from_curve,
)
from coinbot.optimize.space import (
    PRESETS,
    Candidate,
    SearchSpace,
    SpaceError,
    get_path,
    inert_dimensions,
    set_path,
)
from coinbot.optimize.walkforward import (
    WalkForwardOptimizer,
    make_folds,
    slice_htf,
    slice_window,
    time_axis,
)

import synthetic as S


@pytest.fixture
def opt_config():
    cfg = Config()
    cfg.trading.products = ["BTC-USD"]
    cfg.trading.warmup_bars = 250
    cfg.trading.dry_run = False
    cfg.backtest.initial_equity = 10_000.0
    cfg.storage.db_path = ":memory:"
    return cfg


@pytest.fixture
def tape():
    """Long enough for a couple of folds on top of a 250-bar warm-up."""
    return {"BTC-USD": S.trending_up(n=1200, seed=4)}


# --------------------------------------------------------------------------- #
# Search space
# --------------------------------------------------------------------------- #


def test_every_preset_is_valid_and_nonempty():
    for name in PRESETS:
        space = SearchSpace.preset(name)
        assert space.size >= 1
        assert space.dimensions


def test_unknown_config_path_is_rejected():
    with pytest.raises(SpaceError, match="unknown config path"):
        SearchSpace({"risk.no_such_setting": [1.0]})


def test_misspelled_path_names_the_known_fields():
    with pytest.raises(SpaceError, match="atr_stop_multiple"):
        SearchSpace({"risk.atr_stop_mult": [1.0]})


@pytest.mark.parametrize(
    "path",
    [
        "risk.max_drawdown_pct",
        "risk.daily_loss_limit_pct",
        "risk.max_consecutive_losses",
        "risk.halt_on_drawdown",
        "execution.taker_fee_bps",
        "backtest.fee_bps",
        "api.api_key",
    ],
)
def test_risk_limits_and_costs_cannot_be_optimized(path):
    """An optimizer pointed at return always wants the brakes off."""
    with pytest.raises(SpaceError, match="may not be optimized"):
        SearchSpace({path: [0.99]})


def test_unknown_strategy_parameter_is_rejected():
    with pytest.raises(SpaceError, match="no parameter"):
        SearchSpace({"strategy.params.trend_follow.not_a_param": [1.0]})
    with pytest.raises(SpaceError, match="unknown strategy"):
        SearchSpace({"strategy.params.nonesuch.stop_atr": [1.0]})


def test_grid_covers_every_combination():
    space = SearchSpace({
        "risk.atr_stop_multiple": [1.5, 2.5],
        "risk.max_open_positions": [2, 3, 4],
    })
    assert space.size == 6
    assert len({c.key() for c in space.grid()}) == 6


def test_sampling_is_deterministic_and_distinct():
    space = SearchSpace.preset("broad")
    first = space.sample(25, seed=7)
    second = space.sample(25, seed=7)
    assert [c.values for c in first] == [c.values for c in second]
    assert len({c.key() for c in first}) == 25
    assert [c.values for c in space.sample(25, seed=8)] != [c.values for c in first]


def test_sampling_returns_the_whole_grid_when_it_is_smaller_than_the_budget():
    space = SearchSpace({"risk.max_open_positions": [1, 2]})
    assert len(space.sample(50)) == 2


def test_candidate_applies_without_touching_the_original(opt_config):
    original = opt_config.risk.atr_stop_multiple
    tuned = Candidate({"risk.atr_stop_multiple": 3.3}).apply(opt_config)
    assert tuned.risk.atr_stop_multiple == 3.3
    assert opt_config.risk.atr_stop_multiple == original


def test_strategy_parameters_round_trip(opt_config):
    set_path(opt_config, "strategy.params.trend_follow.adx_min", 31.0)
    assert opt_config.strategy.params["trend_follow"]["adx_min"] == 31.0
    assert get_path(opt_config, "strategy.params.trend_follow.adx_min") == 31.0


def test_unset_strategy_parameter_reads_the_class_default(opt_config):
    assert get_path(opt_config, "strategy.params.trend_follow.adx_min") == 22.0


def test_inert_exit_parameters_are_detected(opt_config):
    """These do nothing unless the portfolio-level override is on."""
    space = SearchSpace({"risk.take_profit_r": [1.0, 3.0]})
    assert inert_dimensions(space, opt_config) == ["risk.take_profit_r"]

    opt_config.risk.override_strategy_exits = True
    assert inert_dimensions(space, opt_config) == []


def test_optimizer_refuses_a_search_that_cannot_change_anything(opt_config):
    with pytest.raises(SpaceError, match="no effect"):
        WalkForwardOptimizer(opt_config, SearchSpace({"risk.take_profit_r": [1.0, 3.0]}))


# --------------------------------------------------------------------------- #
# Objectives
# --------------------------------------------------------------------------- #


def test_a_run_with_no_trades_never_wins():
    """Otherwise "did nothing" beats every losing candidate and takes the fold."""
    for objective in OBJECTIVES.values():
        assert objective.score(Metrics()) == float("-inf")


def test_thin_samples_are_discounted():
    objective = get_objective("risk_adjusted")
    many = Metrics(total_trades=100, total_return=0.3, max_drawdown=0.1)
    few = Metrics(total_trades=5, total_return=0.3, max_drawdown=0.1)
    assert objective.score(few) < objective.score(many)


def test_deep_drawdowns_are_penalized():
    objective = get_objective("risk_adjusted")
    shallow = Metrics(total_trades=100, total_return=0.25, max_drawdown=0.10)
    deep = Metrics(total_trades=100, total_return=0.40, max_drawdown=0.45)
    assert objective.score(shallow) > objective.score(deep)


def test_total_wipeout_scores_nothing():
    objective = get_objective("risk_adjusted")
    assert objective.score(Metrics(total_trades=100, total_return=-1.0, max_drawdown=1.0)) <= 0


def test_infinite_ratios_are_finite_and_still_penalized():
    objective = get_objective("profit_factor")
    lossless = Metrics(total_trades=40, profit_factor=float("inf"), max_drawdown=0.5)
    score = objective.score(lossless)
    assert math.isfinite(score)
    assert score < objective.score(
        Metrics(total_trades=40, profit_factor=float("inf"), max_drawdown=0.0)
    )


def test_unknown_objective_is_rejected():
    with pytest.raises(ValueError, match="unknown objective"):
        get_objective("maximum_profit_please")


# --------------------------------------------------------------------------- #
# Fold construction — the part that keeps train and test apart
# --------------------------------------------------------------------------- #


def test_folds_never_test_on_training_data(tape):
    for fold in make_folds(tape, folds=3, warmup=250):
        assert fold.train_start < fold.train_end <= fold.test_start < fold.test_end


def test_test_windows_are_consecutive_and_do_not_overlap(tape):
    folds = make_folds(tape, folds=3, warmup=250)
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.test_end <= later.test_start


def test_rolling_windows_move_but_anchored_ones_do_not(tape):
    rolling = make_folds(tape, folds=3, warmup=250, anchored=False)
    anchored = make_folds(tape, folds=3, warmup=250, anchored=True)
    assert len({f.train_start for f in rolling}) == len(rolling)
    assert len({f.train_start for f in anchored}) == 1


def test_too_little_history_is_an_error_not_a_silent_tiny_fold():
    short = {"BTC-USD": S.trending_up(n=300, seed=1)}
    with pytest.raises(ValueError, match="not enough history"):
        make_folds(short, folds=4, warmup=250)


def test_warmup_bars_come_from_before_the_window_not_out_of_it(tape):
    fold = make_folds(tape, folds=2, warmup=250)[0]
    sliced = slice_window(tape, fold.test_start, fold.test_end, warmup=250)
    bars = sliced["BTC-USD"]

    # Exactly 250 bars precede the window, and they are real history.
    before = [c for c in bars if c.start < fold.test_start]
    assert len(before) == 250
    # Nothing from beyond the window's end is included.
    assert all(c.start < fold.test_end for c in bars)
    # And every bar of the window itself is present.
    original = [
        c for c in tape["BTC-USD"] if fold.test_start <= c.start < fold.test_end
    ]
    assert len(bars) - 250 == len(original)


def test_a_window_without_enough_preceding_history_is_refused(tape):
    axis = time_axis(tape)
    assert slice_window(tape, axis[10], axis[100], warmup=250) is None


def test_higher_timeframe_bars_never_come_from_the_future(tape):
    htf = {"BTC-USD": S.trending_up(n=400, seed=6)}
    cutoff = htf["BTC-USD"][200].start
    sliced = slice_htf(htf, cutoff)
    assert all(c.start < cutoff for c in sliced["BTC-USD"])
    assert slice_htf(None, cutoff) is None


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #


def make_trades(n, seed=0, win_rate=0.45, win=180.0, loss=-110.0):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        pnl = win if rng.random() < win_rate else loss
        out.append(
            TradeRecord(
                product_id="BTC-USD", strategy="trend_follow", side=Side.BUY,
                size=Decimal("0.1"), entry_price=Decimal("100"),
                exit_price=Decimal("101"), pnl=Decimal(str(pnl)),
                fees=Decimal("1"), opened_at=0.0, closed_at=300.0,
            )
        )
    return out


def test_monte_carlo_is_deterministic_for_a_seed():
    trades = make_trades(80, seed=3)
    a = monte_carlo(trades, starting_equity=10_000.0, simulations=200, seed=1)
    b = monte_carlo(trades, starting_equity=10_000.0, simulations=200, seed=1)
    assert a.return_percentiles == b.return_percentiles
    assert a.drawdown_percentiles == b.drawdown_percentiles


def test_monte_carlo_percentiles_are_ordered():
    result = monte_carlo(make_trades(80, seed=3), starting_equity=10_000.0,
                         simulations=500, seed=1)
    returns = [result.return_percentiles[p] for p in sorted(result.return_percentiles)]
    drawdowns = [result.drawdown_percentiles[p] for p in sorted(result.drawdown_percentiles)]
    assert returns == sorted(returns)
    assert drawdowns == sorted(drawdowns)


def test_the_observed_drawdown_is_usually_milder_than_the_tail():
    """The whole point: one realized path understates what the process can do."""
    result = monte_carlo(make_trades(120, seed=5), starting_equity=10_000.0,
                         simulations=1000, seed=2)
    assert result.drawdown_percentiles[95] > result.observed_max_drawdown


def test_a_losing_edge_mostly_loses():
    losing = make_trades(100, seed=4, win_rate=0.3, win=100.0, loss=-150.0)
    result = monte_carlo(losing, starting_equity=10_000.0, simulations=500, seed=1)
    assert result.probability_of_loss > 0.9
    assert result.median_return < 0


def test_monte_carlo_handles_no_trades():
    result = monte_carlo([], starting_equity=10_000.0)
    assert result.simulations == 0
    assert result.probability_of_loss == 0.0


def test_monte_carlo_rejects_nonsense_equity():
    with pytest.raises(ValueError):
        monte_carlo(make_trades(5), starting_equity=0.0)


# --------------------------------------------------------------------------- #
# Deflated Sharpe
# --------------------------------------------------------------------------- #


def normal_returns(n, mean, sd, seed):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_more_trials_raise_the_bar():
    returns = normal_returns(1500, 0.0008, 0.01, seed=1)
    few = deflated_sharpe(returns, trials=2)
    many = deflated_sharpe(returns, trials=1000)
    assert many.expected_max_sharpe > few.expected_max_sharpe
    assert many.probability < few.probability


def test_a_real_edge_survives_many_trials():
    strong = normal_returns(2000, 0.0015, 0.01, seed=2)
    assert deflated_sharpe(strong, trials=500).passes


def test_noise_does_not_survive():
    noise = normal_returns(2000, 0.0, 0.01, seed=3)
    assert not deflated_sharpe(noise, trials=500).passes


def test_tightly_clustered_trials_lower_the_bar():
    """Best-of-500 means less when the 500 were nearly identical."""
    returns = normal_returns(1500, 0.0008, 0.01, seed=4)
    wide = deflated_sharpe(returns, trials=500,
                           trial_sharpes=normal_returns(500, 0.05, 0.05, seed=5))
    tight = deflated_sharpe(returns, trials=500,
                            trial_sharpes=normal_returns(500, 0.05, 0.001, seed=6))
    assert tight.expected_max_sharpe < wide.expected_max_sharpe


def test_degenerate_inputs_do_not_explode():
    assert deflated_sharpe([], trials=10).probability == 0.0
    assert deflated_sharpe([0.01] * 50, trials=10).probability == 0.0


def test_returns_from_curve():
    curve = [(0, Decimal("100")), (1, Decimal("110")), (2, Decimal("99"))]
    returns = returns_from_curve(curve)
    assert returns[0] == pytest.approx(0.10)
    assert returns[1] == pytest.approx(-0.10)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_walk_forward_runs_and_reports_out_of_sample(opt_config, tape):
    space = SearchSpace({"strategy.min_signal_strength": [0.3, 0.6]})
    result = WalkForwardOptimizer(opt_config, space, folds=2).run(tape)

    assert len(result.folds) == 2
    assert result.oos_metrics is not None
    assert result.baseline_metrics is not None
    # Every fold picked a value the search was actually offered.
    for fold in result.folds:
        assert fold.best.values["strategy.min_signal_strength"] in (0.3, 0.6)
    assert result.total_trials == sum(f.evaluated for f in result.folds)

    report = result.render()
    assert "STITCHED OUT-OF-SAMPLE RESULT" in report
    assert "Parameter stability" in report

    payload = result.to_dict()
    assert payload["out_of_sample"] is not None
    assert set(payload["consensus"]) <= {"strategy.min_signal_strength"}


@pytest.mark.slow
def test_parallel_evaluation_matches_serial(opt_config, tape):
    """Workers must not change the answer, only how long it takes to get it."""
    space = SearchSpace({"strategy.min_signal_strength": [0.3, 0.6]})
    serial = WalkForwardOptimizer(opt_config, space, folds=2, jobs=1).run(tape)
    parallel = WalkForwardOptimizer(opt_config, space, folds=2, jobs=2).run(tape)

    assert serial.consensus().values == parallel.consensus().values
    assert [f.best.values for f in serial.folds] == [f.best.values for f in parallel.folds]
    assert serial.oos_metrics.total_return == parallel.oos_metrics.total_return


@pytest.mark.slow
def test_consensus_prefers_what_the_folds_agreed_on(opt_config, tape):
    space = SearchSpace({"strategy.min_signal_strength": [0.3, 0.6]})
    result = WalkForwardOptimizer(opt_config, space, folds=3).run(tape)
    consensus = result.consensus()
    chosen = [f.best.values["strategy.min_signal_strength"] for f in result.folds]
    # The shipped value is one the search actually tested, not an average of two.
    assert consensus.values["strategy.min_signal_strength"] in chosen


@pytest.mark.slow
def test_stability_flags_disagreement_between_folds(opt_config, tape):
    space = SearchSpace({"strategy.min_signal_strength": [0.3, 0.6]})
    result = WalkForwardOptimizer(opt_config, space, folds=3).run(tape)
    stability = result.parameter_stability()
    entry = stability["strategy.min_signal_strength"]
    assert len(entry["values"]) == len(result.folds)
    assert entry["spread"] >= 0.0
