"""Backtest runner.

Drives the real :class:`~coinbot.engine.bot.TradingBot` over recorded candles,
so the strategies, regime detector, risk manager and execution logic exercised
here are byte-for-byte the ones that run live.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..config import Config
from ..data.features import annualization_factor
from ..engine.bot import TradingBot
from ..execution.paper import PaperBroker
from ..logging_setup import get_logger
from ..models import Candle, Product, TradeRecord
from ..util.numeric import to_decimal
from .data import HistoricalBroker
from .metrics import Metrics, compute_metrics

log = get_logger("backtest")


@dataclass
class BacktestResult:
    """Everything a run produces."""

    metrics: Metrics
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[tuple] = field(default_factory=list)
    bars: int = 0
    regime_counts: Dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        report = self.metrics.render()
        if self.regime_counts:
            total = sum(self.regime_counts.values()) or 1
            lines = ["  Time spent in each regime:"]
            for name, count in sorted(self.regime_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {name:<18} {count:>6} bars  ({count / total:.1%})")
            report += "\n" + "\n".join(lines) + "\n" + "=" * 62
        return report


class Backtester:
    """Replays historical data through the live trading loop."""

    def __init__(self, config: Config) -> None:
        # Copied so a backtest can safely tweak settings (dry_run, fees)
        # without mutating the caller's configuration.
        self.cfg = copy.deepcopy(config)
        self.cfg.trading.dry_run = False  # paper broker: orders must be simulated
        self.cfg.execution.taker_fee_bps = config.backtest.fee_bps
        self.cfg.execution.maker_fee_bps = max(0.0, config.backtest.fee_bps - 20.0)
        self.cfg.execution.slippage_bps = config.backtest.slippage_bps

    def run(
        self,
        candles: Dict[str, List[Candle]],
        *,
        htf_candles: Optional[Dict[str, List[Candle]]] = None,
        products: Optional[Dict[str, Product]] = None,
        progress_every: int = 0,
    ) -> BacktestResult:
        """Replay ``candles`` and return the result."""
        if not candles:
            raise ValueError("no candles supplied to the backtester")

        starting_equity = to_decimal(self.cfg.backtest.initial_equity)
        warmup = max(self.cfg.trading.warmup_bars, 250)

        shortest = min(len(bars) for bars in candles.values())
        if shortest <= warmup + 10:
            raise ValueError(
                f"not enough history: {shortest} bars but warm-up alone needs "
                f"{warmup}. Supply more data or lower trading.warmup_bars."
            )

        paper = PaperBroker(
            starting_cash=starting_equity,
            quote_currency=self.cfg.trading.quote_currency,
            exec_cfg=self.cfg.execution,
            products=products,
        )
        broker = HistoricalBroker(
            candles, paper=paper, htf_candles=htf_candles,
            products=products, start_index=warmup,
        )

        # Trade only what we actually have data for.
        self.cfg.trading.products = [
            pid for pid in self.cfg.trading.products if pid in candles
        ] or list(candles)

        bot = TradingBot(self.cfg, broker, store=None)
        bot.prepare()

        equity_curve: List[tuple] = []
        regime_counts: Dict[str, int] = {}
        bars_in_market = 0
        bars = 0

        while True:
            try:
                bot.run_once()
            except Exception:
                log.exception("backtest iteration failed at bar %d; continuing", broker.cursor)

            equity = bot._equity()
            equity_curve.append((broker.current_time, equity))
            if bot.book.open_positions():
                bars_in_market += 1
            for state in bot.state.values():
                name = state.regime.regime.value
                regime_counts[name] = regime_counts.get(name, 0) + 1

            bars += 1
            if progress_every and bars % progress_every == 0:
                log.info(
                    "backtest %.0f%% (bar %d/%d) equity %.2f, %d trades",
                    broker.progress * 100, broker.cursor, broker.length,
                    equity, len(bot.book.trades),
                )

            if not broker.advance():
                break

        # Close anything still open at the final price, so the reported result
        # reflects realized outcomes rather than a favourable open position.
        self._liquidate(bot, broker)

        # Liquidation moved cash; re-read balances before the final mark or the
        # closing equity is computed from a pre-liquidation snapshot.
        bot._refresh_account()
        final_equity = bot._equity()
        equity_curve.append((broker.current_time, final_equity))

        metrics = compute_metrics(
            equity_curve,
            bot.book.trades,
            starting_equity=starting_equity,
            bars_per_year=annualization_factor(self.cfg.trading.bar_seconds),
            bars_elapsed=bars,
            bars_in_market=bars_in_market,
        )
        return BacktestResult(
            metrics=metrics,
            trades=list(bot.book.trades),
            equity_curve=equity_curve,
            bars=bars,
            regime_counts=regime_counts,
        )

    def _liquidate(self, bot: TradingBot, broker: HistoricalBroker) -> None:
        """Flatten every open position at the last available price."""
        from ..models import Side

        for position in list(bot.book.open_positions()):
            state = bot.state.get(position.product_id)
            if state is None or state.product is None:
                continue
            side = Side.SELL if position.is_long else Side.BUY
            price = broker.paper.mark(position.product_id)
            if price is None:
                continue
            result = bot.execution.close_position(
                product=state.product, side=side, size=abs(position.size),
                quote=None, reference_price=price,
                strategy=position.strategy, reason="backtest end: liquidated",
                urgent=True,
            )
            for fill in result.fills:
                trade = bot.book.apply_fill(
                    fill, strategy=position.strategy,
                    regime=position.regime_at_entry,
                    exit_reason="backtest end", update_cash=False,
                )
                if trade is not None:
                    log.debug("liquidated %s: pnl %+.2f", trade.product_id, trade.pnl)


__all__ = ["BacktestResult", "Backtester"]
