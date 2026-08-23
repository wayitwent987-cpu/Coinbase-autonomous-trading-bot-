"""Walk-forward optimization: fit on one slice of history, score on the next.

A backtest tuned on the same data it is judged on tells you nothing. Given
enough parameter combinations, *something* always fits — and the better the
number, the more of it tends to be noise the search memorized. The only
defence is to hold data back.

This module cuts real history into consecutive folds::

    |------- train 1 -------|-- test 1 --|
              |------- train 2 -------|-- test 2 --|
                        |------- train 3 -------|-- test 3 --|

For each fold it searches the space on the training window only, takes the
single best configuration, and runs it once on the test window that follows —
data the search never saw. The test windows are then stitched into one
continuous out-of-sample equity curve.

**That stitched curve is the deliverable.** It answers the only question worth
asking: if I had re-tuned this bot periodically using nothing but the data
available at the time, what would have happened next? Everything else in the
report is diagnostics for judging how much to believe it.

Three diagnostics matter most:

* **Degradation** — the gap between in-sample and out-of-sample scores. A large
  gap means the search fitted noise. Some gap is normal; a reversal in sign is
  a red flag.
* **Parameter stability** — whether each fold picked similar values. A
  parameter that lands somewhere different every fold has no stable optimum,
  and the value you ship is arbitrary.
* **Fold agreement** — how many test windows made money. Three losing folds and
  one enormous winner is a different proposition from four modest winners, even
  at the same total.
"""

from __future__ import annotations

import bisect
import copy
import dataclasses
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..backtest.engine import Backtester, BacktestResult
from ..backtest.metrics import Metrics, compute_metrics
from ..config import Config
from ..data.features import annualization_factor
from ..logging_setup import get_logger
from ..models import Candle, Product, TradeRecord
from ..util.numeric import to_decimal
from ..util.timing import iso
from .objective import Objective, get_objective
from .space import Candidate, SearchSpace, SpaceError, inert_dimensions

log = get_logger("optimize")

CandleMap = Dict[str, List[Candle]]

#: Shortest training window worth searching. Below this the "best"
#: configuration is chosen from a sample too small to distinguish parameters
#: from noise, and every fold picks a different winner for no reason.
MIN_TRAIN_BARS = 100

#: Shortest test window worth scoring. A handful of bars produces a return
#: that is one trade's luck.
MIN_TEST_BARS = 20


# --------------------------------------------------------------------------- #
# Splitting history
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fold:
    """One train/test pair, expressed as timestamps on the shared time axis."""

    index: int
    train_start: int
    train_end: int          # exclusive; also the test window's start
    test_end: int           # exclusive

    @property
    def test_start(self) -> int:
        return self.train_end

    def render(self) -> str:
        return (
            f"fold {self.index}: train {iso(self.train_start)} .. {iso(self.train_end)} "
            f"| test {iso(self.test_start)} .. {iso(self.test_end)}"
        )


def time_axis(candles: CandleMap) -> List[int]:
    """The sorted union of every product's bar timestamps."""
    stamps = set()
    for bars in candles.values():
        stamps.update(c.start for c in bars)
    return sorted(stamps)


def make_folds(
    candles: CandleMap,
    *,
    folds: int = 4,
    test_fraction: float = 0.25,
    anchored: bool = False,
    warmup: int = 250,
) -> List[Fold]:
    """Cut ``candles`` into ``folds`` consecutive train/test pairs.

    ``test_fraction`` is the share of each fold's total span given to testing.
    ``anchored`` keeps every training window starting at the beginning of the
    data (an expanding window) instead of rolling it forward; use it when you
    believe older data still applies, and the rolling default when you think
    the market has changed since.

    The first ``warmup`` bars are reserved: indicators need history before the
    first fold's training window can begin, and taking it from inside the
    window would mean training on bars the strategy could not have acted on.
    """
    if folds < 1:
        raise ValueError("need at least one fold")
    if not 0.05 <= test_fraction <= 0.9:
        raise ValueError("test_fraction must be between 0.05 and 0.9")

    axis = time_axis(candles)
    usable = axis[warmup:]

    # Each fold advances by one test window, so the span covered is
    # train + folds * test. Solve for the test length in bars.
    n = len(usable)
    needed = MIN_TRAIN_BARS + folds * MIN_TEST_BARS
    if n < needed:
        raise ValueError(
            f"not enough history for {folds} folds: {len(axis)} bars, of which "
            f"{warmup} are reserved for indicator warm-up, leaving {n}. "
            f"{folds} folds need at least {needed} ({MIN_TRAIN_BARS} to train on "
            f"plus {MIN_TEST_BARS} per test window). Fetch more data, use fewer "
            f"folds, or lower trading.warmup_bars."
        )

    test_len = int(n * test_fraction / (folds * test_fraction + (1.0 - test_fraction)))
    test_len = max(test_len, MIN_TEST_BARS)
    train_len = n - folds * test_len
    if train_len < MIN_TRAIN_BARS:
        raise ValueError(
            f"not enough history: {folds} folds at test_fraction={test_fraction:g} "
            f"leaves only {train_len} bars to train on, below the {MIN_TRAIN_BARS} "
            f"needed for a search to mean anything. Fetch more data, use fewer "
            f"folds, or lower --test-fraction."
        )

    out: List[Fold] = []
    for i in range(folds):
        train_start_idx = 0 if anchored else i * test_len
        train_end_idx = train_len + i * test_len
        test_end_idx = min(train_end_idx + test_len, n)
        if test_end_idx - train_end_idx < MIN_TEST_BARS:
            break
        out.append(
            Fold(
                index=i + 1,
                train_start=usable[train_start_idx],
                train_end=usable[train_end_idx],
                test_end=usable[test_end_idx - 1] + 1,
            )
        )
    return out


def slice_window(
    candles: CandleMap,
    start: int,
    end: int,
    *,
    warmup: int,
) -> Optional[CandleMap]:
    """Bars in ``[start, end)`` preceded by ``warmup`` bars of real history.

    The warm-up bars are taken from *before* the window, never from inside it,
    so the strategy is scored on every bar of the window and the indicators are
    already spun up on the first one. Returns ``None`` when some product lacks
    enough preceding history for the window to be evaluated honestly.
    """
    out: CandleMap = {}
    for product_id, bars in candles.items():
        starts = [c.start for c in bars]
        lo = bisect.bisect_left(starts, start)
        hi = bisect.bisect_left(starts, end)
        warm_lo = lo - warmup
        if warm_lo < 0 or hi - lo < 2:
            return None
        out[product_id] = bars[warm_lo:hi]
    return out or None


def slice_htf(candles: Optional[CandleMap], end: int) -> Optional[CandleMap]:
    """Higher-timeframe bars strictly before ``end``.

    No warm-up arithmetic is needed here: the replay serves higher-timeframe
    candles by timestamp, so handing it everything up to the window's end gives
    the same view a live bot would have had, with nothing from the future.
    """
    if not candles:
        return None
    out: CandleMap = {}
    for product_id, bars in candles.items():
        kept = [c for c in bars if c.start < end]
        if kept:
            out[product_id] = kept
    return out or None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class Evaluation:
    """One candidate scored on one window."""

    candidate: Candidate
    metrics: Metrics
    score: float
    failed: Optional[str] = None


@dataclass
class FoldResult:
    """What a single train/test pair produced."""

    fold: Fold
    best: Candidate
    train_score: float
    train_metrics: Metrics
    test_score: float
    test_metrics: Metrics
    baseline_test_metrics: Optional[Metrics] = None
    #: Configurations attempted on the training window, including any that
    #: errored or never traded — the honest cost of the search.
    evaluated: int = 0
    #: Of those, the ones that produced a rankable score.
    usable: int = 0
    ranking: List[Tuple[str, float]] = field(default_factory=list)
    test_trades: List[TradeRecord] = field(default_factory=list)
    test_equity_curve: List[tuple] = field(default_factory=list)
    test_bars: int = 0
    test_bars_in_market: int = 0
    baseline_trades: List[TradeRecord] = field(default_factory=list)
    baseline_equity_curve: List[tuple] = field(default_factory=list)
    baseline_bars: int = 0
    baseline_bars_in_market: int = 0
    #: Sharpe of every configuration tried on the training window. The
    #: multiple-testing adjustment needs the spread across trials, not just
    #: how many there were: the best of 500 near-identical configurations is a
    #: much weaker claim than the best of 500 that differed wildly.
    trial_sharpes: List[float] = field(default_factory=list)

    @property
    def degradation(self) -> float:
        """In-sample score minus out-of-sample score, as a fraction of in-sample.

        1.0 means the entire in-sample edge vanished; above 1.0 means the
        configuration lost money out of sample.
        """
        if not math.isfinite(self.train_score) or self.train_score == 0:
            return float("nan")
        return (self.train_score - self.test_score) / abs(self.train_score)


@dataclass
class WalkForwardResult:
    """The full walk-forward run."""

    objective: str
    folds: List[FoldResult] = field(default_factory=list)
    #: Metrics of the stitched out-of-sample curve — the headline number.
    oos_metrics: Optional[Metrics] = None
    #: The same windows traded with the unmodified config, for comparison.
    baseline_metrics: Optional[Metrics] = None
    oos_equity_curve: List[tuple] = field(default_factory=list)
    candidates_per_fold: int = 0
    elapsed_s: float = 0.0
    space: Dict[str, List[Any]] = field(default_factory=dict)
    #: Seconds per bar of the data this ran on, for annualization.
    bar_seconds: int = 0

    # ------------------------------------------------------------------ #

    def trial_sharpes(self) -> List[float]:
        """Every configuration's Sharpe, across all folds, on a per-bar basis.

        :class:`~coinbot.backtest.metrics.Metrics` reports Sharpe annualized;
        the deflated-Sharpe calculation works per observation, so these are
        divided back down by the same annualization factor.
        """
        scale = math.sqrt(annualization_factor(self.bar_seconds)) if self.bar_seconds else 1.0
        if scale <= 0:
            scale = 1.0
        return [f_sharpe / scale for fold in self.folds for f_sharpe in fold.trial_sharpes]

    @property
    def total_trials(self) -> int:
        """Configurations evaluated across the whole run."""
        return sum(f.evaluated for f in self.folds)

    @property
    def profitable_folds(self) -> int:
        return sum(1 for f in self.folds if f.test_metrics.total_return > 0)

    @property
    def median_degradation(self) -> float:
        values = [f.degradation for f in self.folds if math.isfinite(f.degradation)]
        return statistics.median(values) if values else float("nan")

    def parameter_stability(self) -> Dict[str, Dict[str, Any]]:
        """Per-parameter: the value each fold chose, and how much they varied.

        ``spread`` is the coefficient of variation for numbers, or the share of
        folds disagreeing with the modal choice for everything else. Low means
        the folds agreed and the value is worth shipping; high means the search
        found no stable optimum and the number you ship is close to arbitrary.
        """
        out: Dict[str, Dict[str, Any]] = {}
        paths = sorted({p for f in self.folds for p in f.best.values})
        for path in paths:
            chosen = [f.best.values.get(path) for f in self.folds]
            present = [v for v in chosen if v is not None]
            entry: Dict[str, Any] = {"values": chosen}
            numeric = [
                float(v) for v in present
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            if len(numeric) == len(present) and len(numeric) > 1:
                mean = statistics.fmean(numeric)
                stdev = statistics.pstdev(numeric)
                entry["mean"] = mean
                entry["spread"] = (stdev / abs(mean)) if mean else 0.0
            else:
                # Booleans and strings have no median, so "agreement" is how
                # many folds landed on the most common choice.
                counts = Counter(repr(v) for v in present)
                modal, agree = counts.most_common(1)[0] if counts else (None, 0)
                entry["modal"] = modal
                entry["spread"] = 1.0 - (agree / len(present)) if present else float("nan")
            out[path] = entry
        return out

    def consensus(self) -> Candidate:
        """A single configuration drawn from what the folds agreed on.

        The median for numbers, the most common choice for everything else, and
        later folds break ties — recent history is the better guide when the
        folds disagree. This is what ``--write-config`` saves, in preference to
        the last fold's pick, because one fold's winner is one sample.
        """
        stability = self.parameter_stability()
        values: Dict[str, Any] = {}
        for path, entry in stability.items():
            present = [v for v in entry["values"] if v is not None]
            if not present:
                continue
            numeric = [
                v for v in present
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            if len(numeric) == len(present):
                median = statistics.median(numeric)
                # Snap to the nearest value the search actually tried, so the
                # shipped config is a point that was tested rather than an
                # interpolation between two that were.
                values[path] = min(present, key=lambda v: (abs(v - median), -present.index(v)))
            else:
                winner = Counter(repr(v) for v in present).most_common(1)[0][0]
                # Reversed: on a tie, the most recent fold's choice wins.
                values[path] = next(v for v in reversed(present) if repr(v) == winner)
        return Candidate(values)

    # ------------------------------------------------------------------ #

    def render(self) -> str:
        lines = [
            "=" * 74,
            "WALK-FORWARD OPTIMIZATION",
            "=" * 74,
            f"  Objective            {self.objective}",
            f"  Folds                {len(self.folds)}",
            f"  Configs per fold     {self.candidates_per_fold}",
            f"  Total backtests      {sum(f.evaluated + 2 for f in self.folds)}",
            f"  Wall time            {self.elapsed_s:,.1f}s",
            "-" * 74,
            "  Per fold (test windows are data the search never saw):",
            "",
            f"    {'#':<3} {'test window':<24} {'in-samp':>9} {'out-samp':>9} "
            f"{'return':>9} {'maxDD':>7} {'trades':>7}",
        ]
        for f in self.folds:
            window = f"{iso(f.fold.test_start)[:10]}..{iso(f.fold.test_end)[:10]}"
            lines.append(
                f"    {f.fold.index:<3} {window:<24} {_num(f.train_score):>9} "
                f"{_num(f.test_score):>9} {f.test_metrics.total_return:>8.2%} "
                f"{f.test_metrics.max_drawdown:>6.1%} {f.test_metrics.total_trades:>7d}"
            )

        lines += ["-" * 74, "  Honesty checks:"]
        lines.append(
            f"    Profitable test folds     {self.profitable_folds}/{len(self.folds)}"
            + ("   <-- fewer than half" if self.profitable_folds * 2 < len(self.folds) else "")
        )
        degradation = self.median_degradation
        lines.append(
            f"    Median degradation        {degradation:>6.1%}"
            if math.isfinite(degradation)
            else "    Median degradation           n/a"
        )
        if math.isfinite(degradation) and degradation > 1.0:
            lines.append(
                "      The entire in-sample edge disappeared out of sample. "
                "That is a fitted result, not an edge."
            )
        elif math.isfinite(degradation) and degradation > 0.6:
            lines.append(
                "      Most of the in-sample edge did not survive. Treat the "
                "out-of-sample figure as the optimistic case."
            )

        stability = self.parameter_stability()
        if stability:
            lines += ["-" * 74, "  Parameter stability across folds:", ""]
            for path, entry in sorted(stability.items(), key=lambda kv: -_spread(kv[1])):
                chosen = " ".join(_fmt(v) for v in entry["values"])
                spread = _spread(entry)
                flag = "  <-- unstable" if spread > 0.35 else ""
                lines.append(f"    {path.split('.')[-1]:<26} {chosen:<28} spread {spread:>5.0%}{flag}")

        if self.oos_metrics is not None:
            lines += [
                "-" * 74,
                "  STITCHED OUT-OF-SAMPLE RESULT (the number that matters):",
                "",
                f"    Total return          {self.oos_metrics.total_return:>9.2%}",
                f"    Max drawdown          {self.oos_metrics.max_drawdown:>9.2%}",
                f"    Sharpe                {self.oos_metrics.sharpe:>10.2f}",
                f"    Profit factor         {self.oos_metrics.profit_factor:>10.2f}",
                f"    Trades                {self.oos_metrics.total_trades:>10d}",
                f"    Fees paid             {self.oos_metrics.total_fees:>10,.2f}",
            ]
        if self.baseline_metrics is not None:
            lines += [
                "",
                f"    Untuned config, same windows: {self.baseline_metrics.total_return:>8.2%} "
                f"return, {self.baseline_metrics.max_drawdown:.1%} drawdown, "
                f"{self.baseline_metrics.total_trades} trades",
            ]
            if (
                self.oos_metrics is not None
                and self.oos_metrics.total_return <= self.baseline_metrics.total_return
            ):
                lines.append(
                    "      Tuning did not beat leaving the defaults alone. Ship the defaults."
                )

        lines.append("=" * 74)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "candidates_per_fold": self.candidates_per_fold,
            "elapsed_s": self.elapsed_s,
            "space": {k: list(v) for k, v in self.space.items()},
            "profitable_folds": self.profitable_folds,
            "median_degradation": self.median_degradation,
            "consensus": self.consensus().values,
            "parameter_stability": self.parameter_stability(),
            "out_of_sample": self.oos_metrics.to_dict() if self.oos_metrics else None,
            "baseline": self.baseline_metrics.to_dict() if self.baseline_metrics else None,
            "folds": [
                {
                    "index": f.fold.index,
                    "train_start": f.fold.train_start,
                    "train_end": f.fold.train_end,
                    "test_end": f.fold.test_end,
                    "best": f.best.values,
                    "train_score": f.train_score,
                    "test_score": f.test_score,
                    "degradation": f.degradation,
                    "evaluated": f.evaluated,
                    "test": f.test_metrics.to_dict(),
                }
                for f in self.folds
            ],
        }


def _num(value: float) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.3f}"


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _spread(entry: Dict[str, Any]) -> float:
    spread = entry.get("spread", float("nan"))
    return spread if isinstance(spread, float) and math.isfinite(spread) else 0.0


# --------------------------------------------------------------------------- #
# The optimizer
# --------------------------------------------------------------------------- #


class WalkForwardOptimizer:
    """Runs the fit-then-validate loop over historical candles."""

    def __init__(
        self,
        config: Config,
        space: SearchSpace,
        *,
        objective: Optional[str] = None,
        folds: int = 4,
        test_fraction: float = 0.25,
        anchored: bool = False,
        budget: Optional[int] = None,
        seed: int = 0,
        jobs: int = 1,
    ) -> None:
        self.cfg = copy.deepcopy(config)
        self.space = space
        self.objective: Objective = get_objective(objective)
        self.folds = folds
        self.test_fraction = test_fraction
        self.anchored = anchored
        self.budget = budget
        self.seed = seed
        self.jobs = max(1, jobs)

        # Refuse a search whose knobs cannot move the result. The run would
        # take just as long, report a "winner", and mean nothing.
        inert = inert_dimensions(space, self.cfg)
        if inert:
            raise SpaceError(
                f"these parameters have no effect under the current config: "
                f"{', '.join(inert)}. Every strategy supplies its own stop and "
                f"target, so the portfolio-level multiples are unused until "
                f"risk.override_strategy_exits is enabled. Set it in your "
                f"config, or add it to the search space."
            )

    # ------------------------------------------------------------------ #

    def run(
        self,
        candles: CandleMap,
        *,
        htf_candles: Optional[CandleMap] = None,
        products: Optional[Dict[str, Product]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> WalkForwardResult:
        started = time.time()
        warmup = max(self.cfg.trading.warmup_bars, 250)
        splits = make_folds(
            candles,
            folds=self.folds,
            test_fraction=self.test_fraction,
            anchored=self.anchored,
            warmup=warmup,
        )
        candidates = self.space.candidates(self.budget, seed=self.seed)
        result = WalkForwardResult(
            objective=self.objective.name,
            candidates_per_fold=len(candidates),
            space={k: list(v) for k, v in self.space.dimensions.items()},
            bar_seconds=self.cfg.trading.bar_seconds,
        )

        say = on_progress or (lambda message: log.info("%s", message))
        say(
            f"{len(splits)} folds x {len(candidates)} configs "
            f"= {len(splits) * len(candidates)} training backtests"
        )

        for split in splits:
            say(split.render())
            fold_result = self._run_fold(
                split, candidates, candles, htf_candles, products, say
            )
            if fold_result is not None:
                result.folds.append(fold_result)

        if not result.folds:
            raise ValueError(
                "no fold produced a usable result; the windows are probably too "
                "short for the configured warm-up"
            )

        result.oos_metrics, result.oos_equity_curve = self._stitch(
            [
                (f.test_equity_curve, f.test_trades, f.test_bars, f.test_bars_in_market)
                for f in result.folds
            ]
        )
        if all(f.baseline_equity_curve for f in result.folds):
            result.baseline_metrics, _ = self._stitch(
                [
                    (
                        f.baseline_equity_curve, f.baseline_trades,
                        f.baseline_bars, f.baseline_bars_in_market,
                    )
                    for f in result.folds
                ]
            )
        result.elapsed_s = time.time() - started
        return result

    # ------------------------------------------------------------------ #

    def _run_fold(
        self,
        split: Fold,
        candidates: Sequence[Candidate],
        candles: CandleMap,
        htf_candles: Optional[CandleMap],
        products: Optional[Dict[str, Product]],
        say: Callable[[str], None],
    ) -> Optional[FoldResult]:
        warmup = max(self.cfg.trading.warmup_bars, 250)
        train = slice_window(candles, split.train_start, split.train_end, warmup=warmup)
        test = slice_window(candles, split.test_start, split.test_end, warmup=warmup)
        if train is None or test is None:
            say(f"  fold {split.index}: skipped, not enough history for the window")
            return None

        train_htf = slice_htf(htf_candles, split.train_end)
        test_htf = slice_htf(htf_candles, split.test_end)

        evaluations = self._evaluate_all(candidates, train, train_htf, products)
        usable = [e for e in evaluations if e.failed is None and math.isfinite(e.score)]
        if not usable:
            say(f"  fold {split.index}: no configuration traded; skipped")
            return None

        usable.sort(key=lambda e: e.score, reverse=True)
        winner = usable[0]

        test_eval = self._evaluate(winner.candidate, test, test_htf, products, keep=True)
        if test_eval[0].failed is not None:
            say(f"  fold {split.index}: winner failed on the test window; skipped")
            return None
        test_result = test_eval[1]
        # The same window traded with the config as shipped. Without this the
        # report cannot answer the question that decides whether to adopt the
        # tuned values at all: did the tuning beat changing nothing?
        baseline, baseline_result = self._evaluate(
            Candidate({}), test, test_htf, products, keep=True
        )

        say(
            f"  fold {split.index}: best {winner.candidate.render()} "
            f"| in-sample {winner.score:.3f} -> out-of-sample {test_eval[0].score:.3f} "
            f"({test_eval[0].metrics.total_return:+.2%} over {test_eval[0].metrics.total_trades} trades)"
        )

        return FoldResult(
            fold=split,
            best=winner.candidate,
            train_score=winner.score,
            train_metrics=winner.metrics,
            test_score=test_eval[0].score,
            test_metrics=test_eval[0].metrics,
            baseline_test_metrics=baseline.metrics if baseline.failed is None else None,
            evaluated=len(evaluations),
            usable=len(usable),
            ranking=[(e.candidate.render(), e.score) for e in usable[:10]],
            trial_sharpes=[
                e.metrics.sharpe for e in usable if math.isfinite(e.metrics.sharpe)
            ],
            test_trades=list(test_result.trades) if test_result else [],
            test_equity_curve=list(test_result.equity_curve) if test_result else [],
            test_bars=test_result.bars if test_result else 0,
            test_bars_in_market=test_result.bars_in_market if test_result else 0,
            baseline_trades=list(baseline_result.trades) if baseline_result else [],
            baseline_equity_curve=(
                list(baseline_result.equity_curve) if baseline_result else []
            ),
            baseline_bars=baseline_result.bars if baseline_result else 0,
            baseline_bars_in_market=(
                baseline_result.bars_in_market if baseline_result else 0
            ),
        )

    def _evaluate_all(
        self,
        candidates: Sequence[Candidate],
        window: CandleMap,
        htf: Optional[CandleMap],
        products: Optional[Dict[str, Product]],
    ) -> List[Evaluation]:
        if self.jobs > 1 and len(candidates) > 1:
            parallel = self._evaluate_parallel(candidates, window, htf, products)
            if parallel is not None:
                return parallel
        return [self._evaluate(c, window, htf, products)[0] for c in candidates]

    def _evaluate_parallel(
        self,
        candidates: Sequence[Candidate],
        window: CandleMap,
        htf: Optional[CandleMap],
        products: Optional[Dict[str, Product]],
    ) -> Optional[List[Evaluation]]:
        """Score candidates across processes; ``None`` if that is unavailable.

        Backtests are pure Python and CPU-bound, so threads would serialize on
        the GIL. The window is sent to each worker once at start-up rather than
        with every task — the candles dwarf the parameters being varied.
        """
        try:
            from concurrent.futures import ProcessPoolExecutor
        except ImportError:  # pragma: no cover - stdlib, but be defensive
            return None

        payload = (self.cfg, window, htf, products, self.objective.name)
        try:
            with ProcessPoolExecutor(
                max_workers=self.jobs, initializer=_worker_init, initargs=(payload,)
            ) as pool:
                scored = list(pool.map(_worker_score, [c.values for c in candidates]))
        except (OSError, RuntimeError, NotImplementedError) as exc:
            # Sandboxes and some frozen environments cannot fork. Falling back
            # to one process is slower but produces identical numbers.
            log.warning("parallel evaluation unavailable (%s); using one process", exc)
            return None

        out: List[Evaluation] = []
        for candidate, (score, metrics, failed) in zip(candidates, scored):
            out.append(
                Evaluation(
                    candidate=candidate,
                    metrics=metrics or Metrics(),
                    score=score,
                    failed=failed,
                )
            )
        return out

    def _evaluate(
        self,
        candidate: Candidate,
        window: CandleMap,
        htf: Optional[CandleMap],
        products: Optional[Dict[str, Product]],
        *,
        keep: bool = False,
    ) -> Tuple[Evaluation, Optional[BacktestResult]]:
        """Run one configuration over one window."""
        try:
            cfg = candidate.apply(self.cfg)
        except Exception as exc:
            return Evaluation(candidate, Metrics(), float("-inf"), f"invalid: {exc}"), None

        try:
            result = Backtester(cfg).run(window, htf_candles=htf, products=products)
        except Exception as exc:
            log.debug("candidate %s failed: %s", candidate.render(), exc)
            return Evaluation(candidate, Metrics(), float("-inf"), str(exc)), None

        score = self.objective.score(result.metrics)
        evaluation = Evaluation(candidate, result.metrics, score)
        return evaluation, (result if keep else None)

    # ------------------------------------------------------------------ #

    def _stitch(
        self, folds: Sequence[Tuple[List[tuple], List[TradeRecord], int, int]]
    ) -> Tuple[Metrics, List[tuple]]:
        """Chain the test windows into one compounding equity curve.

        Each fold's backtest starts from the same nominal equity, so they are
        chained multiplicatively: fold 2 begins with whatever fold 1 finished
        with. Trade P&L is rescaled by the same running multiplier, so that a
        profit factor computed across folds compares like with like instead of
        weighting every fold as though it started from the initial stake.
        """
        start = to_decimal(self.cfg.backtest.initial_equity)
        curve: List[tuple] = []
        trades: List[TradeRecord] = []
        multiplier = Decimal(1)
        bars = 0
        in_market = 0

        for equity_curve, fold_trades, fold_bars, fold_in_market in folds:
            if not equity_curve:
                continue
            opening = to_decimal(equity_curve[0][1]) or start
            for timestamp, value in equity_curve:
                curve.append((timestamp, to_decimal(value) * multiplier))
            for trade in fold_trades:
                trades.append(
                    dataclasses.replace(
                        trade,
                        pnl=trade.pnl * multiplier,
                        fees=trade.fees * multiplier,
                    )
                )
            closing = to_decimal(equity_curve[-1][1])
            multiplier *= (closing / opening) if opening else Decimal(1)
            bars += fold_bars
            in_market += fold_in_market

        metrics = compute_metrics(
            curve,
            trades,
            starting_equity=start,
            bars_per_year=annualization_factor(self.cfg.trading.bar_seconds),
            bars_elapsed=bars or max(1, len(curve) - 1),
            bars_in_market=in_market,
        )
        return metrics, curve


# --------------------------------------------------------------------------- #
# Worker-process plumbing
# --------------------------------------------------------------------------- #

_WORKER: Dict[str, Any] = {}


def _worker_init(payload: tuple) -> None:
    cfg, window, htf, products, objective = payload
    _WORKER.update(
        cfg=cfg, window=window, htf=htf, products=products,
        objective=get_objective(objective),
    )


def _worker_score(values: Dict[str, Any]) -> Tuple[float, Optional[Metrics], Optional[str]]:
    candidate = Candidate(values)
    try:
        cfg = candidate.apply(_WORKER["cfg"])
        result = Backtester(cfg).run(
            _WORKER["window"],
            htf_candles=_WORKER["htf"],
            products=_WORKER["products"],
        )
    except Exception as exc:  # reported, never raised: one bad point must not
        return float("-inf"), None, str(exc)  # abort the whole search
    objective: Objective = _WORKER["objective"]
    return objective.score(result.metrics), result.metrics, None


__all__ = [
    "MIN_TEST_BARS",
    "MIN_TRAIN_BARS",
    "Evaluation",
    "Fold",
    "FoldResult",
    "WalkForwardOptimizer",
    "WalkForwardResult",
    "make_folds",
    "slice_htf",
    "slice_window",
    "time_axis",
]
