"""The trading bot: one loop, executed once per bar close.

Order of operations within a bar matters, and it is deliberate:

1. **Refresh data** — candles and quotes.
2. **Reconcile orders** — collect fills from last bar before deciding anything,
   so the position the bot reasons about is the position it actually holds.
3. **Check circuit breakers** — before any new risk is taken.
4. **Manage open positions** — stops and exits first. Getting out is always
   evaluated before getting in, so a bar that is both an exit for one product
   and an entry for another frees capital in the right order.
5. **Look for entries** — regime, strategies, risk sizing, order.
6. **Persist** — so a restart resumes from here.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..config import Config
from ..data.candles import CandleSeries
from ..data.features import (
    FeatureParams,
    FeatureSet,
    annualization_factor,
    compute_features,
)
from ..execution.broker import Broker
from ..execution.engine import ExecutionEngine
from ..logging_setup import get_logger
from ..models import (
    AccountSnapshot,
    Candle,
    Fill,
    Position,
    Product,
    Quote,
    Regime,
    RegimeState,
    Side,
    SignalAction,
    TradeRecord,
)
from ..portfolio.book import PositionBook
from ..regime.detector import RegimeDetector
from ..risk.manager import RiskManager
from ..storage.state import StateStore
from ..strategies.base import StrategyContext
from ..strategies.router import Decision, StrategyRouter
from ..util.numeric import to_decimal
from ..util.timing import humanize_duration, iso, now_ts, sleep_until_next_bar

log = get_logger("bot")


@dataclass
class ProductState:
    """Everything the bot tracks per product."""

    product_id: str
    product: Optional[Product] = None
    candles: Optional[CandleSeries] = None
    htf_candles: Optional[CandleSeries] = None
    features: Optional[FeatureSet] = None
    htf_features: Optional[FeatureSet] = None
    regime: RegimeState = field(default_factory=RegimeState)
    last_bar_ts: int = 0
    errors: int = 0


class TradingBot:
    """Wires the pieces together and runs the loop."""

    def __init__(
        self,
        config: Config,
        broker: Broker,
        *,
        store: Optional[StateStore] = None,
    ) -> None:
        self.cfg = config
        self.broker = broker
        self.store = store

        bar_seconds = config.trading.bar_seconds
        self.bars_per_year = annualization_factor(bar_seconds)
        self.feature_params = FeatureParams.from_regime_config(
            config.regime, self.bars_per_year
        )

        self.detector = RegimeDetector(config.regime)
        self.router = StrategyRouter(config.strategy)
        self.risk = RiskManager(config.risk)
        self.execution = ExecutionEngine(broker, config.execution, config.risk)
        self.book = PositionBook(quote_currency=config.trading.quote_currency)

        self.state: Dict[str, ProductState] = {
            pid: ProductState(product_id=pid) for pid in config.trading.products
        }
        self.account = AccountSnapshot(quote_currency=config.trading.quote_currency)
        self._running = False
        self._stop_requested = False
        self._loop_count = 0
        self._started_at = 0.0

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

    def prepare(self) -> None:
        """Load products, warm up history and restore any saved state."""
        log.info("preparing bot: %s", self.cfg.summary())
        trading = self.cfg.trading

        for pid, state in self.state.items():
            try:
                state.product = self.broker.get_product(pid)
            except Exception as exc:
                log.error("could not load product %s: %s", pid, exc)
                continue
            if not state.product.tradable:
                log.warning("%s is not currently tradable; it will be skipped", pid)

            state.candles = CandleSeries(pid, trading.bar_seconds, trading.max_candles)
            state.htf_candles = CandleSeries(
                pid, trading.htf_granularity_enum.seconds, max(200, trading.warmup_bars // 4)
            )
            self._warm_up(state)

        self._refresh_account()
        self.book.cash = self.account.cash
        self.book.stats.starting_equity = self.account.equity
        self.book.stats.peak_equity = self.account.equity

        if self.store is not None:
            self._restore_state()

        ready = sum(1 for s in self.state.values() if self._is_ready(s))
        log.info(
            "ready: %d/%d products warmed up, equity %.2f %s",
            ready, len(self.state), self.account.equity, self.cfg.trading.quote_currency,
        )
        if ready == 0:
            raise RuntimeError(
                "no product has enough history to trade; check connectivity and product ids"
            )

    def _warm_up(self, state: ProductState) -> None:
        trading = self.cfg.trading
        try:
            candles = self.broker.get_candles(
                state.product_id, trading.granularity_enum, bars=trading.warmup_bars
            )
            state.candles.replace_all(candles)
            htf = self.broker.get_candles(
                state.product_id, trading.htf_granularity_enum, bars=200
            )
            state.htf_candles.replace_all(htf)
            log.info(
                "%s warmed up: %d bars (%s) + %d htf bars",
                state.product_id, len(state.candles), trading.granularity, len(state.htf_candles),
            )
            gaps = state.candles.gaps()
            if gaps:
                missing = sum(g[2] for g in gaps)
                log.warning(
                    "%s history has %d gap(s), %d bars missing; indicators may be skewed",
                    state.product_id, len(gaps), missing,
                )
        except Exception as exc:
            log.error("warm-up failed for %s: %s", state.product_id, exc)
            state.errors += 1

    def _is_ready(self, state: ProductState) -> bool:
        return (
            state.product is not None
            and state.candles is not None
            and state.candles.ready(self.feature_params.min_bars)
        )

    def _restore_state(self) -> None:
        """Reload positions and risk state after a restart."""
        assert self.store is not None
        positions = self.store.load_positions()
        for pid, position in positions.items():
            if pid not in self.state:
                log.warning(
                    "stored position in %s is no longer configured; leaving it alone", pid
                )
                continue
            self.book.positions[pid] = position
            log.info(
                "restored position: %s %s @ %s (stop %s, strategy %s)",
                pid, position.size, position.entry_price,
                position.stop_price, position.strategy or "?",
            )

        for trade in self.store.load_trades(limit=200):
            self.book.trades.append(trade)
            self.book.stats.record_trade(trade)

        peak = self.store.get_meta("peak_equity")
        if peak:
            # Keep the historical peak so a restart cannot quietly reset the
            # drawdown breaker by treating today's lower equity as the peak.
            self.book.stats.peak_equity = max(
                self.book.stats.peak_equity, to_decimal(peak)
            )
            log.info("restored equity peak: %.2f", self.book.stats.peak_equity)

        halt = self.store.get_meta("halt_reason")
        if halt:
            log.critical("bot was halted before restart: %s", halt)
            self.risk.halt(f"restored halt: {halt}")

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def run(self, *, max_iterations: Optional[int] = None) -> None:
        """Run until stopped. Handles SIGINT/SIGTERM for a clean shutdown."""
        self.prepare()
        self._install_signal_handlers()
        self._running = True
        self._started_at = now_ts()

        mode = "DRY-RUN" if self.cfg.trading.dry_run else "LIVE"
        log.info("=== %s trading started (%s broker) ===", mode, self.broker.mode)

        try:
            while self._running and not self._stop_requested:
                if max_iterations is not None and self._loop_count >= max_iterations:
                    log.info("reached max_iterations=%d; stopping", max_iterations)
                    break
                try:
                    self.run_once()
                except Exception:
                    log.exception("loop iteration failed; continuing")
                self._loop_count += 1
                self._sleep_to_next_bar()
        finally:
            self.shutdown()

    def run_once(self) -> None:
        """One full pass. Public so the backtester and tests can drive it."""
        started = time.monotonic()

        self._refresh_market_data()
        fills = self._reconcile_orders()
        self._refresh_account()

        equity = self._equity()
        breaker = self.risk.check_circuit_breakers(self.book, equity)
        halted = not breaker
        if halted:
            self._handle_halt(breaker.reason)

        self.risk.tick_cooldowns()
        self.book.tick_bars()

        # Exits first: a bar can both free capital and want to deploy it.
        #
        # Open positions are managed even while halted. A breaker stops the bot
        # taking *new* risk; abandoning the risk it already carries — no
        # trailing stops, no exits — would be the opposite of protective.
        self._manage_open_positions()
        if not halted:
            self._look_for_entries(equity)

        # Re-read balances after trading. Mixing a pre-trade cash figure with
        # a post-trade position book reports an exit bar as a huge phantom
        # drawdown — it counts neither the position nor the cash it became.
        self._refresh_account()
        final_equity = self._equity()

        self._persist(final_equity)
        log.debug(
            "loop %d done in %.2fs (%d fills, equity %.2f)",
            self._loop_count, time.monotonic() - started, len(fills), final_equity,
        )

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #

    def _refresh_market_data(self) -> None:
        """Pull new candles and recompute features for each product."""
        trading = self.cfg.trading
        self.broker.sync()

        for state in self.state.values():
            if state.candles is None or state.product is None:
                continue
            try:
                # A small overlap re-fetches the last few bars so a revised
                # closing candle replaces the provisional one we already saw.
                fresh = self.broker.get_candles(
                    state.product_id, trading.granularity_enum, bars=10
                )
                state.candles.extend(fresh)

                if self._loop_count % 12 == 0 or len(state.htf_candles) == 0:
                    htf = self.broker.get_candles(
                        state.product_id, trading.htf_granularity_enum, bars=5
                    )
                    state.htf_candles.extend(htf)
            except Exception as exc:
                state.errors += 1
                log.warning("market data refresh failed for %s: %s", state.product_id, exc)
                continue

            closed = state.candles.closed_candles()
            if not closed or len(closed) < self.feature_params.min_bars:
                continue

            state.features = compute_features(state.product_id, closed, self.feature_params)
            htf_closed = state.htf_candles.closed_candles() if state.htf_candles else []
            if len(htf_closed) >= 60:
                state.htf_features = compute_features(
                    state.product_id, htf_closed, self.feature_params
                )

            if state.features is not None:
                state.regime = self.detector.detect(state.features, state.htf_features)
                if state.features.ts != state.last_bar_ts:
                    state.last_bar_ts = state.features.ts
                    log.info(
                        "%s bar %s close=%.6g regime=%s",
                        state.product_id, iso(state.features.ts),
                        state.features.close, state.regime,
                    )
                self.book.mark(state.product_id, to_decimal(state.features.close))

    def _reconcile_orders(self) -> List[Fill]:
        """Collect fills and apply them to the position book.

        Each fill arrives with the intent that produced it, so a limit entry
        that rested for several bars still lands with the right strategy tag
        and the stop/target the risk manager sized it against.
        """
        quotes = self._quotes()
        try:
            events = self.execution.reconcile(quotes)
        except Exception:
            log.exception("order reconciliation failed")
            return []

        for event in events:
            fill = event.fill
            existing = self.book.get(fill.product_id)
            strategy = event.strategy or (existing.strategy if existing else "")
            regime = _regime_or(event.regime, self.state.get(fill.product_id))

            trade = self.book.apply_fill(
                fill,
                strategy=strategy,
                regime=regime,
                exit_reason=event.reason if event.intent == "exit" else "",
                update_cash=False,
            )
            if trade is not None:
                self._on_trade_closed(trade)
                continue

            # A newly opened position inherits the stop and target that the
            # order was sized against.
            position = self.book.get(fill.product_id)
            if position is not None and event.is_entry:
                position.strategy = strategy or position.strategy
                if event.stop_price is not None:
                    position.stop_price = event.stop_price
                if event.take_profit is not None:
                    position.take_profit = event.take_profit
                if self.store is not None:
                    self.store.save_position(position)

        return [e.fill for e in events]

    def _on_trade_closed(self, trade: TradeRecord) -> None:
        log.info(
            "closed %s %s: pnl %+.2f (%.2f%%) in %s [%s] %s",
            trade.side.value, trade.product_id, trade.pnl, trade.return_pct * 100,
            humanize_duration(trade.duration_s), trade.strategy, trade.exit_reason,
        )
        self.risk.on_trade_closed(trade)
        if self.store is not None:
            self.store.record_trade(trade)
            self.store.delete_position(trade.product_id)

    def _refresh_account(self) -> None:
        try:
            self.account = self.broker.get_account(self.cfg.trading.quote_currency)
            self.book.cash = self.account.cash
        except Exception as exc:
            log.warning("account refresh failed: %s", exc)

    def _equity(self) -> Decimal:
        """Prefer the venue's own valuation; fall back to our book."""
        if self.account.positions_value or self.account.cash:
            return self.account.cash + self.book.positions_value
        return self.book.equity()

    def _manage_open_positions(self) -> None:
        """Update stops and close anything that has hit an exit condition."""
        quotes = self._quotes()

        for state in self.state.values():
            position = self.book.get(state.product_id)
            if position is None or state.features is None or state.product is None:
                continue

            if self.risk.update_stops(position, state.features):
                log.debug(
                    "%s stop moved to %s (bars held %d)",
                    state.product_id, position.stop_price, position.bars_held,
                )

            last_bar = state.candles.last if state.candles else None
            check = self.risk.check_exit(position, state.features, bar=last_bar)
            reason = check.reason
            urgent = check.urgent

            if not check.should_exit:
                # No risk-based exit; ask the strategies.
                decision = self._decide(state, position)
                if decision.is_exit:
                    reason = decision.reason
                    urgent = False
                else:
                    continue

            self._close_position(state, position, reason, urgent, quotes)

    def _look_for_entries(self, equity: Decimal) -> None:
        quotes = self._quotes()
        candles_by_product = {
            pid: s.candles.candles for pid, s in self.state.items() if s.candles
        }

        for state in self.state.values():
            if not self._is_ready(state) or state.features is None:
                continue
            if self.book.has_position(state.product_id):
                continue
            if self.execution.has_working(state.product_id):
                continue

            decision = self._decide(state, None)
            if not decision.is_entry:
                continue

            direction = decision.direction
            if direction < 0 and not self.cfg.trading.allow_shorts:
                continue

            quote = quotes.get(state.product_id)
            verdict = self.risk.check_entry(
                state.product_id, direction, self.book, equity,
                features=state.features, regime=state.regime.regime,
                spread_bps=quote.spread_bps if quote else None,
                candles_by_product=candles_by_product,
            )
            if not verdict:
                log.debug("%s entry blocked: %s", state.product_id, verdict.reason)
                continue

            self._open_position(state, decision, equity, verdict.scale, quote)

    # ------------------------------------------------------------------ #
    # Decisions and orders
    # ------------------------------------------------------------------ #

    def _decide(self, state: ProductState, position: Optional[Position]) -> Decision:
        ctx = StrategyContext(
            product_id=state.product_id,
            features=state.features,
            regime=state.regime,
            htf=state.htf_features,
            position=position,
            allow_shorts=self.cfg.trading.allow_shorts,
        )
        return self.router.evaluate(ctx)

    def _open_position(
        self,
        state: ProductState,
        decision: Decision,
        equity: Decimal,
        scale: float,
        quote: Optional[Quote],
    ) -> None:
        assert state.product is not None and state.features is not None
        price = to_decimal(state.features.close)
        direction = decision.direction

        stop = self.risk.initial_stop(price, direction, state.features, decision.stop_price)
        sizing = self.risk.size_position(
            product=state.product,
            price=price,
            direction=direction,
            stop_price=stop,
            conviction=decision.sized_conviction,
            equity=equity,
            available_cash=self.account.available_cash or self.book.cash,
            book=self.book,
            features=state.features,
            take_profit=decision.take_profit,
            scale=scale,
            bars_per_year=self.bars_per_year,
        )

        if not sizing.is_tradeable:
            log.debug("%s sizing rejected: %s", state.product_id, sizing.rejected)
            return

        side = Side.BUY if direction > 0 else Side.SELL
        log.info(
            "ENTRY %s %s %s size=%s notional=%.2f risk=%.2f stop=%s target=%s "
            "[%s/%s conv=%.2f] %s",
            side.value, state.product_id, decision.regime.value, sizing.size,
            sizing.notional, sizing.risk_amount, sizing.stop_price, sizing.take_profit,
            decision.strategy, sizing.binding_constraint, decision.sized_conviction,
            decision.reason,
        )

        if self.cfg.trading.dry_run and self.broker.is_live:
            log.warning("dry-run: order suppressed for %s", state.product_id)
            return

        result = self.execution.open_position(
            product=state.product, side=side, size=sizing.size, quote=quote,
            reference_price=price, strategy=decision.strategy, reason=decision.reason,
            stop_price=sizing.stop_price, take_profit=sizing.take_profit,
            regime=decision.regime.value,
        )
        if result.error:
            log.error("entry failed for %s: %s", state.product_id, result.error)
            return
        if result.skipped:
            log.info("entry skipped for %s: %s", state.product_id, result.skipped)
            return
        if result.resting:
            # The order is working. Its stop and target travel with it and are
            # attached by _reconcile_orders when it fills.
            log.info("%s entry resting at %s", state.product_id, result.submitted.limit_price)
            return

        for fill in result.fills:
            self.book.apply_fill(
                fill, strategy=decision.strategy,
                regime=decision.regime, update_cash=False,
            )

        position = self.book.get(state.product_id)
        if position is not None:
            position.strategy = decision.strategy
            position.regime_at_entry = decision.regime
            position.stop_price = sizing.stop_price
            position.take_profit = sizing.take_profit
            if self.store is not None:
                self.store.save_position(position)

    def _close_position(
        self,
        state: ProductState,
        position: Position,
        reason: str,
        urgent: bool,
        quotes: Dict[str, Quote],
    ) -> None:
        assert state.product is not None
        side = Side.SELL if position.is_long else Side.BUY
        size = abs(position.size)
        price = to_decimal(state.features.close) if state.features else position.last_price

        log.info(
            "EXIT %s %s size=%s pnl=%+.2f (%.2f%%) :: %s",
            side.value, state.product_id, size,
            position.unrealized_pnl, position.unrealized_pnl_pct * 100, reason,
        )

        if self.cfg.trading.dry_run and self.broker.is_live:
            log.warning("dry-run: exit order suppressed for %s", state.product_id)
            return

        # A working order on this product would fight the exit for the same
        # inventory, so clear it first.
        if self.execution.has_working(state.product_id):
            self.execution.cancel_all(state.product_id)

        result = self.execution.close_position(
            product=state.product, side=side, size=size,
            quote=quotes.get(state.product_id), reference_price=price,
            strategy=position.strategy, reason=reason, urgent=urgent,
        )
        if result.error:
            log.error("exit failed for %s: %s", state.product_id, result.error)
            return

        for fill in result.fills:
            trade = self.book.apply_fill(
                fill, strategy=position.strategy,
                regime=position.regime_at_entry, exit_reason=reason, update_cash=False,
            )
            if trade is not None:
                self._on_trade_closed(trade)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _quotes(self) -> Dict[str, Quote]:
        try:
            return self.broker.get_quotes(list(self.state))
        except Exception as exc:
            log.debug("quote fetch failed: %s", exc)
            return {}

    def _handle_halt(self, reason: str) -> None:
        """On a breaker trip: stop working orders and flatten if configured."""
        log.critical("risk halt active: %s", reason)
        cancelled = self.execution.cancel_all()
        if cancelled:
            log.warning("cancelled %d working order(s) due to halt", cancelled)
        if self.store is not None:
            self.store.set_meta("halt_reason", reason)
            self.store.record_event("halt", {"reason": reason}, level="CRITICAL")

    def _persist(self, equity: Decimal) -> None:
        if self.store is None:
            return
        try:
            self.store.record_equity(equity, self.book.cash, self.book.gross_exposure)
            self.store.set_meta("peak_equity", str(self.book.stats.peak_equity))
            self.store.set_meta("last_loop", now_ts())
            for position in self.book.open_positions():
                self.store.save_position(position)
        except Exception:
            log.exception("state persistence failed")

    def _sleep_to_next_bar(self) -> None:
        if self._stop_requested:
            return
        interval = self.cfg.trading.loop_interval_s
        if interval > 0:
            time.sleep(interval)
        else:
            sleep_until_next_bar(
                self.cfg.trading.bar_seconds, self.cfg.trading.bar_close_offset_s
            )

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # pragma: no cover - signal path
            log.warning("received signal %s; shutting down after this loop", signum)
            self._stop_requested = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not on the main thread

    def stop(self) -> None:
        self._stop_requested = True
        self._running = False

    def shutdown(self) -> None:
        """Cancel working orders and flush state. Open positions are kept."""
        if not self._running:
            return
        self._running = False
        log.info("shutting down...")
        try:
            cancelled = self.execution.cancel_all()
            if cancelled:
                log.info("cancelled %d working order(s)", cancelled)
        except Exception:
            log.exception("failed to cancel working orders during shutdown")

        try:
            self._persist(self._equity())
        except Exception:
            log.exception("failed to persist final state")

        log.info("final state: %s", self.book.summary())
        if self.book.open_positions():
            log.warning(
                "%d position(s) remain open and are NOT auto-closed: %s",
                self.book.open_count,
                ", ".join(p.product_id for p in self.book.open_positions()),
            )
        try:
            self.broker.close()
        except Exception:
            pass
        if self.store is not None:
            self.store.close()

    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, object]:
        """A snapshot suitable for logging or a status command."""
        return {
            "mode": "dry-run" if self.cfg.trading.dry_run else "live",
            "broker": self.broker.mode,
            "loops": self._loop_count,
            "uptime": humanize_duration(now_ts() - self._started_at) if self._started_at else "-",
            "equity": float(self._equity()),
            "cash": float(self.book.cash),
            "open_positions": {
                p.product_id: {
                    "size": str(p.size),
                    "entry": str(p.entry_price),
                    "stop": str(p.stop_price) if p.stop_price else None,
                    "pnl": float(p.unrealized_pnl),
                    "strategy": p.strategy,
                    "bars": p.bars_held,
                }
                for p in self.book.open_positions()
            },
            "regimes": {
                pid: s.regime.regime.value for pid, s in self.state.items()
            },
            "risk": self.risk.status(),
            "trades": self.book.stats.total_trades,
            "win_rate": self.book.stats.win_rate,
        }


def _regime_or(name: str, state: Optional[ProductState]) -> Regime:
    """Regime recorded on the order, falling back to the product's current one."""
    if name:
        try:
            return Regime(name)
        except ValueError:
            pass
    return state.regime.regime if state is not None else Regime.UNKNOWN


__all__ = ["ProductState", "TradingBot"]
