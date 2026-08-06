"""Performance metrics for a completed run.

Reported deliberately as a *set*: total return alone says nothing about
whether a strategy is worth running. Max drawdown, profit factor and trade
count together answer "could I actually have sat through this, and is the
result more than luck?".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..models import TradeRecord


@dataclass
class Metrics:
    """Summary statistics for a run."""

    starting_equity: Decimal = Decimal(0)
    ending_equity: Decimal = Decimal(0)
    total_return: float = 0.0
    annualized_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    volatility: float = 0.0

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_duration_bars: float = 0.0
    max_consecutive_losses: int = 0
    total_fees: Decimal = Decimal(0)
    exposure: float = 0.0

    #: False when the sample is too short for annualization to mean anything.
    annualized_valid: bool = False
    years: float = 0.0

    by_strategy: Dict[str, Dict[str, float]] = field(default_factory=dict)
    by_regime: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def render(self) -> str:
        """A human-readable report."""
        annualized = (
            f"{self.annualized_return:>13.2%}"
            if self.annualized_valid
            else "    n/a (sample too short)"
        )
        calmar = f"{self.calmar:>14.2f}" if self.annualized_valid else "           n/a"
        lines = [
            "=" * 62,
            "BACKTEST RESULTS",
            "=" * 62,
            f"  Period covered      {self.years * 12:>13.1f} months",
            f"  Starting equity     {self.starting_equity:>14,.2f}",
            f"  Ending equity       {self.ending_equity:>14,.2f}",
            f"  Total return        {self.total_return:>13.2%}",
            f"  Annualized return   {annualized}",
            f"  Max drawdown        {self.max_drawdown:>13.2%}",
            f"  Volatility (ann.)   {self.volatility:>13.2%}",
            f"  Sharpe              {self.sharpe:>14.2f}",
            f"  Sortino             {self.sortino:>14.2f}",
            f"  Calmar              {calmar}",
            "-" * 62,
            f"  Trades              {self.total_trades:>14d}",
            f"  Win rate            {self.win_rate:>13.1%}  ({self.wins}W / {self.losses}L)",
            f"  Profit factor       {self.profit_factor:>14.2f}",
            f"  Expectancy/trade    {self.expectancy:>14,.2f}",
            f"  Avg win / avg loss  {self.avg_win:>8,.2f} / {self.avg_loss:,.2f}",
            f"  Best / worst        {self.largest_win:>8,.2f} / {self.largest_loss:,.2f}",
            f"  Max losing streak   {self.max_consecutive_losses:>14d}",
            f"  Avg hold (bars)     {self.avg_duration_bars:>14.1f}",
            f"  Fees paid           {self.total_fees:>14,.2f}",
            f"  Time in market      {self.exposure:>13.1%}",
        ]

        if self.by_strategy:
            lines += ["-" * 62, "  By strategy:"]
            for name, stats in sorted(
                self.by_strategy.items(), key=lambda kv: -kv[1]["pnl"]
            ):
                lines.append(
                    f"    {name:<18} {int(stats['trades']):>4} trades  "
                    f"win {stats['win_rate']:>5.1%}  pnl {stats['pnl']:>+10,.2f}"
                )

        if self.by_regime:
            lines += ["-" * 62, "  By regime at entry:"]
            for name, stats in sorted(
                self.by_regime.items(), key=lambda kv: -kv[1]["pnl"]
            ):
                lines.append(
                    f"    {name:<18} {int(stats['trades']):>4} trades  "
                    f"win {stats['win_rate']:>5.1%}  pnl {stats['pnl']:>+10,.2f}"
                )

        lines.append("=" * 62)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return {
            "starting_equity": float(self.starting_equity),
            "ending_equity": float(self.ending_equity),
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "total_fees": float(self.total_fees),
            "exposure": self.exposure,
            "by_strategy": self.by_strategy,
            "by_regime": self.by_regime,
        }


def compute_metrics(
    equity_curve: Sequence[tuple],
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal,
    bars_per_year: int = 105_120,
    bars_elapsed: Optional[int] = None,
    bars_in_market: int = 0,
) -> Metrics:
    """Build a :class:`Metrics` from an equity curve and the trade list."""
    metrics = Metrics(starting_equity=starting_equity)

    values = [float(v) for _, v in equity_curve] or [float(starting_equity)]
    metrics.ending_equity = Decimal(str(values[-1]))
    start = float(starting_equity) or 1.0
    metrics.total_return = (values[-1] - start) / start

    # --- Drawdown ---
    peak = float("-inf")
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            metrics.max_drawdown = max(metrics.max_drawdown, (peak - value) / peak)

    # --- Return distribution ---
    returns: List[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)

    periods = bars_elapsed if bars_elapsed is not None else max(1, len(values) - 1)
    years = periods / bars_per_year if bars_per_year else 0.0

    if returns:
        mean = math.fsum(returns) / len(returns)
        variance = math.fsum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        std = math.sqrt(variance)
        metrics.volatility = std * math.sqrt(bars_per_year)
        if std > 0:
            metrics.sharpe = (mean / std) * math.sqrt(bars_per_year)
        downside = [r for r in returns if r < 0]
        if downside:
            d_var = math.fsum(r * r for r in downside) / len(downside)
            d_std = math.sqrt(d_var)
            if d_std > 0:
                metrics.sortino = (mean / d_std) * math.sqrt(bars_per_year)
        elif mean > 0:
            metrics.sortino = float("inf")

    # Annualizing a short sample produces spectacular nonsense: a 4% gain over
    # 250 five-minute bars compounds to billions of percent. Below a month of
    # data the figure is left at zero and the report says so, rather than
    # printing a number nobody should act on.
    metrics.annualized_valid = years >= (1.0 / 12.0)
    if metrics.annualized_valid and metrics.total_return > -1.0:
        metrics.annualized_return = (1.0 + metrics.total_return) ** (1.0 / years) - 1.0
        if metrics.max_drawdown > 0:
            metrics.calmar = metrics.annualized_return / metrics.max_drawdown
    metrics.years = years

    # --- Trade statistics ---
    metrics.total_trades = len(trades)
    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]
    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.win_rate = len(wins) / len(trades) if trades else 0.0

    gross_profit = math.fsum(float(t.pnl) for t in wins)
    gross_loss = abs(math.fsum(float(t.pnl) for t in losses))
    if gross_loss > 0:
        metrics.profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        metrics.profit_factor = float("inf")

    if trades:
        metrics.expectancy = math.fsum(float(t.pnl) for t in trades) / len(trades)
        metrics.total_fees = sum((t.fees for t in trades), Decimal(0))
        bar_seconds = 365 * 24 * 3600 / bars_per_year if bars_per_year else 0
        if bar_seconds > 0:
            metrics.avg_duration_bars = (
                math.fsum(t.duration_s for t in trades) / len(trades) / bar_seconds
            )
    metrics.avg_win = gross_profit / len(wins) if wins else 0.0
    metrics.avg_loss = -gross_loss / len(losses) if losses else 0.0
    metrics.largest_win = max((float(t.pnl) for t in wins), default=0.0)
    metrics.largest_loss = min((float(t.pnl) for t in losses), default=0.0)

    streak = worst_streak = 0
    for trade in trades:
        if trade.is_win:
            streak = 0
        else:
            streak += 1
            worst_streak = max(worst_streak, streak)
    metrics.max_consecutive_losses = worst_streak

    if periods > 0:
        metrics.exposure = min(1.0, bars_in_market / periods)

    metrics.by_strategy = _group_stats(trades, lambda t: t.strategy or "unknown")
    metrics.by_regime = _group_stats(trades, lambda t: t.regime.value)
    return metrics


def _group_stats(trades: Sequence[TradeRecord], key) -> Dict[str, Dict[str, float]]:
    grouped: Dict[str, List[TradeRecord]] = {}
    for trade in trades:
        grouped.setdefault(key(trade), []).append(trade)

    out: Dict[str, Dict[str, float]] = {}
    for name, items in grouped.items():
        wins = sum(1 for t in items if t.is_win)
        out[name] = {
            "trades": float(len(items)),
            "wins": float(wins),
            "win_rate": wins / len(items) if items else 0.0,
            "pnl": math.fsum(float(t.pnl) for t in items),
        }
    return out


__all__ = ["Metrics", "compute_metrics"]
