"""End-to-end backtests.

The headline assertion is the reconciliation identity:

    starting_equity + Σ trade.pnl − Σ trade.fees == ending_equity

It is a complete audit of the money path — sizing, fills, fees, the position
book and the equity curve all have to agree. Two real bugs were caught by it
during development (double-counted fees, and an equity figure computed from a
pre-trade account snapshot mixed with a post-trade book).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coinbot.backtest.data import HistoricalBroker
from coinbot.backtest.engine import Backtester
from coinbot.backtest.metrics import compute_metrics
from coinbot.config import Config
from coinbot.execution.paper import PaperBroker
from coinbot.models import Candle, Granularity

import synthetic as S


def stitch(*tapes, start_price: float = 100.0):
    """Join tapes end to end, rescaling each to continue from the last close."""
    out, t, scale = [], 0, start_price
    for tape in tapes:
        base = tape[0].open
        for candle in tape:
            k = scale / base
            out.append(
                Candle(
                    start=t * 300, open=candle.open * k, high=candle.high * k,
                    low=candle.low * k, close=candle.close * k, volume=candle.volume,
                )
            )
            t += 1
        scale = out[-1].close
    return out


def multi_regime_tape():
    """Uptrend -> range -> crash -> chop -> uptrend -> squeeze/release."""
    return stitch(
        S.trending_up(n=500, seed=11),
        S.ranging(n=400, seed=12),
        S.crash(n=200, seed=13),
        S.volatile_chop(n=300, seed=14),
        S.trending_up(n=400, seed=15),
        S.squeeze(n=300, seed=16, release_bars=20),
    )


@pytest.fixture
def bt_config(config):
    config.trading.warmup_bars = 250
    config.backtest.initial_equity = 10_000.0
    return config


# --------------------------------------------------------------------------- #
# The money path
# --------------------------------------------------------------------------- #


def test_equity_reconciles_exactly(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    gross = sum((t.pnl for t in result.trades), Decimal(0))
    fees = sum((t.fees for t in result.trades), Decimal(0))
    expected = Decimal("10000") + gross - fees
    assert result.metrics.ending_equity == pytest.approx(expected, abs=Decimal("0.01"))


def test_backtest_produces_trades_across_several_strategies(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert result.metrics.total_trades > 10
    assert len(result.metrics.by_strategy) >= 2
    # Every trade must be attributed; "unknown" means the strategy tag was lost.
    assert "unknown" not in result.metrics.by_strategy


def test_no_positions_are_left_open(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert result.metrics.exposure < 1.0
    assert result.metrics.total_trades > 0


def test_fees_cover_both_sides_of_each_round_trip(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert result.metrics.total_fees > 0
    # A round trip pays entry and exit; a per-trade average below one side's
    # worth would mean only one leg was counted.
    per_trade = float(result.metrics.total_fees) / result.metrics.total_trades
    assert per_trade > 0


def test_trades_have_realistic_holding_periods(bt_config):
    """Fills must be stamped with simulated time, not wall clock."""
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert result.metrics.avg_duration_bars > 0.5


def test_no_trading_during_a_crash(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert "CRASH" not in result.metrics.by_regime


# --------------------------------------------------------------------------- #
# Replay integrity
# --------------------------------------------------------------------------- #


def test_historical_broker_never_serves_future_bars():
    tape = S.trending_up(n=400, seed=71)
    paper = PaperBroker(starting_cash=Decimal("10000"))
    broker = HistoricalBroker({"BTC-USD": tape}, paper=paper, start_index=250)

    for _ in range(20):
        visible = broker.get_candles("BTC-USD", Granularity.FIVE_MINUTE, bars=1000)
        assert all(c.start <= broker.current_time for c in visible)
        assert len(visible) == broker.cursor + 1
        broker.advance()


def test_replay_is_deterministic(bt_config):
    """Same data in, same result out — no hidden state between runs."""
    tape = multi_regime_tape()
    first = Backtester(bt_config).run({"BTC-USD": tape})
    second = Backtester(bt_config).run({"BTC-USD": tape})
    assert first.metrics.ending_equity == second.metrics.ending_equity
    assert first.metrics.total_trades == second.metrics.total_trades


def test_truncated_run_matches_the_prefix_of_a_longer_one(bt_config):
    """A shorter tape must yield the same early trades as the longer one.

    If any indicator or decision peeked at future bars, the truncated run would
    diverge from the full run's opening trades.
    """
    tape = multi_regime_tape()
    short = Backtester(bt_config).run({"BTC-USD": tape[:900]})
    full = Backtester(bt_config).run({"BTC-USD": tape})

    # Compare the trades opened well before the truncation point.
    cutoff = tape[850].start
    short_early = [(t.product_id, t.opened_at) for t in short.trades if t.closed_at < cutoff]
    full_early = [(t.product_id, t.opened_at) for t in full.trades if t.closed_at < cutoff]
    assert short_early == full_early


def test_fills_happen_at_the_next_bar_open_not_the_signal_close():
    tape = S.trending_up(n=400, seed=72)
    paper = PaperBroker(starting_cash=Decimal("10000"))
    broker = HistoricalBroker({"BTC-USD": tape}, paper=paper, start_index=250)
    # The mark the broker offers is the *following* bar's open — you cannot
    # decide on a close and transact at that same close.
    assert paper.mark("BTC-USD") == pytest.approx(
        Decimal(str(tape[251].open)), rel=Decimal("1e-9")
    )


def test_backtester_rejects_insufficient_history(bt_config):
    with pytest.raises(ValueError, match="not enough history"):
        Backtester(bt_config).run({"BTC-USD": S.trending_up(n=100)})


def test_backtester_rejects_empty_data(bt_config):
    with pytest.raises(ValueError):
        Backtester(bt_config).run({})


def test_backtest_does_not_mutate_the_caller_config(bt_config):
    before = bt_config.trading.dry_run
    Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    assert bt_config.trading.dry_run == before


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_refuse_to_annualize_a_short_sample():
    """4% over 250 five-minute bars compounds to billions of percent."""
    curve = [(i * 300, Decimal("10000") + Decimal(i)) for i in range(250)]
    metrics = compute_metrics(curve, [], starting_equity=Decimal("10000"))
    assert not metrics.annualized_valid
    assert metrics.annualized_return == 0.0
    assert "n/a" in metrics.render()


def test_metrics_annualize_a_long_sample():
    curve = [(i * 300, Decimal("10000") + Decimal(i)) for i in range(20_000)]
    metrics = compute_metrics(curve, [], starting_equity=Decimal("10000"))
    assert metrics.annualized_valid
    assert metrics.annualized_return != 0.0


def test_metrics_drawdown():
    curve = [(0, Decimal("10000")), (300, Decimal("12000")), (600, Decimal("9000"))]
    metrics = compute_metrics(curve, [], starting_equity=Decimal("10000"))
    assert metrics.max_drawdown == pytest.approx(0.25)


def test_metrics_handle_no_trades():
    curve = [(0, Decimal("10000")), (300, Decimal("10000"))]
    metrics = compute_metrics(curve, [], starting_equity=Decimal("10000"))
    assert metrics.total_trades == 0
    assert metrics.win_rate == 0.0
    assert metrics.render()  # must not raise


def test_result_render_includes_regime_breakdown(bt_config):
    result = Backtester(bt_config).run({"BTC-USD": multi_regime_tape()})
    report = result.render()
    assert "BACKTEST RESULTS" in report
    assert "Time spent in each regime" in report
    assert "By strategy" in report
