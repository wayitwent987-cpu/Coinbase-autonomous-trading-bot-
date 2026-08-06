"""Position book: applies fills, tracks PnL, and records completed trades.

All money math here is ``Decimal``. Position sizing in float would drift by
fractions of a cent per fill, and after a few thousand fills the bot's idea of
its own position stops matching the exchange's — which is the kind of bug that
only shows up when it is expensive.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from ..logging_setup import get_logger
from ..models import Fill, Position, Regime, Side, TradeRecord
from ..util.numeric import to_decimal
from ..util.timing import now_ts

log = get_logger("portfolio")


class PortfolioStats:
    """Running performance statistics derived from closed trades."""

    def __init__(self, starting_equity: Decimal) -> None:
        self.starting_equity = starting_equity
        self.peak_equity = starting_equity
        self.max_drawdown = 0.0
        self.realized_pnl = Decimal(0)
        self.fees_paid = Decimal(0)
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
        self.gross_profit = Decimal(0)
        self.gross_loss = Decimal(0)
        self.equity_curve: List[tuple] = []

    def record_trade(self, trade: TradeRecord) -> None:
        """Record a closed trade's *outcome* statistics.

        Deliberately does not touch ``realized_pnl`` or ``fees_paid``: those are
        accumulated per fill in :meth:`PositionBook.apply_fill`, which also
        catches fees on opening fills that never appear in a TradeRecord.
        Adding them here as well would double-count both.
        """
        if trade.is_win:
            self.wins += 1
            self.gross_profit += trade.pnl
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.gross_loss += abs(trade.pnl)
            self.consecutive_losses += 1
            self.max_consecutive_losses = max(
                self.max_consecutive_losses, self.consecutive_losses
            )

    def mark_equity(self, equity: Decimal, ts: Optional[float] = None) -> None:
        self.equity_curve.append((ts if ts is not None else now_ts(), equity))
        if len(self.equity_curve) > 100_000:
            del self.equity_curve[:10_000]
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            drawdown = float((self.peak_equity - equity) / self.peak_equity)
            self.max_drawdown = max(self.max_drawdown, drawdown)

    def drawdown_from_peak(self, equity: Decimal) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, float((self.peak_equity - equity) / self.peak_equity))

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return float(self.gross_profit / self.gross_loss)

    @property
    def expectancy(self) -> Decimal:
        if not self.total_trades:
            return Decimal(0)
        return self.realized_pnl / Decimal(self.total_trades)


class PositionBook:
    """The bot's view of what it holds, and what it has made or lost."""

    def __init__(self, starting_equity: Decimal = Decimal(0), quote_currency: str = "USD") -> None:
        self.quote_currency = quote_currency
        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []
        self.stats = PortfolioStats(starting_equity)
        self.cash = starting_equity

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    def get(self, product_id: str) -> Optional[Position]:
        position = self.positions.get(product_id)
        return position if position and position.is_open else None

    def get_or_create(self, product_id: str) -> Position:
        if product_id not in self.positions:
            self.positions[product_id] = Position(product_id=product_id)
        return self.positions[product_id]

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_open]

    @property
    def open_count(self) -> int:
        return len(self.open_positions())

    def has_position(self, product_id: str) -> bool:
        return self.get(product_id) is not None

    def long_count(self) -> int:
        return sum(1 for p in self.open_positions() if p.is_long)

    def short_count(self) -> int:
        return sum(1 for p in self.open_positions() if p.is_short)

    # ------------------------------------------------------------------ #
    # Valuation
    # ------------------------------------------------------------------ #

    def mark(self, product_id: str, price: Decimal) -> None:
        position = self.positions.get(product_id)
        if position is not None:
            position.last_price = to_decimal(price)

    def mark_all(self, prices: Dict[str, Decimal]) -> None:
        for product_id, price in prices.items():
            self.mark(product_id, price)

    @property
    def positions_value(self) -> Decimal:
        """Signed market value of open positions."""
        return sum((p.size * p.last_price for p in self.open_positions()), Decimal(0))

    @property
    def gross_exposure(self) -> Decimal:
        return sum((p.notional for p in self.open_positions()), Decimal(0))

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((p.unrealized_pnl for p in self.open_positions()), Decimal(0))

    def equity(self, cash: Optional[Decimal] = None) -> Decimal:
        """Cash plus the market value of everything held."""
        base = self.cash if cash is None else cash
        return base + self.positions_value

    # ------------------------------------------------------------------ #
    # Fills
    # ------------------------------------------------------------------ #

    def apply_fill(
        self,
        fill: Fill,
        *,
        strategy: str = "",
        regime: Regime = Regime.UNKNOWN,
        exit_reason: str = "",
        update_cash: bool = True,
    ) -> Optional[TradeRecord]:
        """Apply a fill and return a :class:`TradeRecord` if it closed a trade.

        Handles the four cases that matter: opening, adding to a position,
        reducing it, and flipping through zero to the other side.
        """
        position = self.get_or_create(fill.product_id)
        signed = fill.size if fill.side is Side.BUY else -fill.size
        price = fill.price

        if update_cash:
            # Buying spends cash, selling raises it; fees always cost.
            self.cash -= signed * price
            self.cash -= fill.fee

        position.fees_paid += fill.fee
        position.last_price = price
        self.stats.fees_paid += fill.fee

        record: Optional[TradeRecord] = None
        old_size = position.size
        new_size = old_size + signed

        if old_size == 0:
            # --- Opening a new position ---
            position.size = new_size
            position.entry_price = price
            position.opened_at = fill.ts
            position.realized_pnl = Decimal(0)
            position.strategy = strategy or position.strategy
            position.regime_at_entry = regime
            position.bars_held = 0
            position.trail_anchor = price

        elif (old_size > 0) == (signed > 0):
            # --- Adding to the position: weighted-average the entry ---
            total_cost = position.entry_price * abs(old_size) + price * fill.size
            position.size = new_size
            position.entry_price = total_cost / abs(new_size)

        else:
            # --- Reducing, closing, or flipping ---
            closed_size = min(abs(old_size), fill.size)
            direction = Decimal(1) if old_size > 0 else Decimal(-1)
            pnl = (price - position.entry_price) * closed_size * direction
            position.realized_pnl += pnl
            self.stats.realized_pnl += pnl

            # Charge the trade for *both* sides of the round trip. Reporting
            # only the exit fee halves the apparent cost of trading, which
            # flatters every strategy — the entry fee was already paid and is
            # sitting in position.fees_paid.
            closed_fraction = closed_size / abs(old_size)
            trade_fees = position.fees_paid * closed_fraction
            position.fees_paid -= trade_fees

            record = TradeRecord(
                product_id=fill.product_id,
                strategy=position.strategy or strategy,
                side=Side.BUY if old_size > 0 else Side.SELL,
                size=closed_size,
                entry_price=position.entry_price,
                exit_price=price,
                pnl=pnl,
                fees=trade_fees,
                opened_at=position.opened_at or fill.ts,
                closed_at=fill.ts,
                regime=position.regime_at_entry,
                exit_reason=exit_reason,
            )
            self.trades.append(record)
            self.stats.record_trade(record)

            position.size = new_size
            if new_size == 0:
                self._flatten(position)
            elif (new_size > 0) != (old_size > 0):
                # Flipped through zero: the remainder is a brand-new position.
                position.entry_price = price
                position.opened_at = fill.ts
                position.realized_pnl = Decimal(0)
                position.strategy = strategy or position.strategy
                position.regime_at_entry = regime
                position.bars_held = 0
                position.trail_anchor = price
                position.stop_price = None
                position.take_profit = None

        return record

    def _flatten(self, position: Position) -> None:
        position.size = Decimal(0)
        position.entry_price = Decimal(0)
        position.fees_paid = Decimal(0)
        position.opened_at = None
        position.stop_price = None
        position.take_profit = None
        position.trail_anchor = None
        position.bars_held = 0
        position.strategy = ""
        position.regime_at_entry = Regime.UNKNOWN

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #

    def tick_bars(self, product_ids: Optional[Iterable[str]] = None) -> None:
        """Advance the holding-period counter used by the time stop."""
        targets = set(product_ids) if product_ids is not None else None
        for position in self.open_positions():
            if targets is None or position.product_id in targets:
                position.bars_held += 1

    def set_stops(
        self,
        product_id: str,
        stop: Optional[Decimal] = None,
        target: Optional[Decimal] = None,
    ) -> None:
        position = self.get(product_id)
        if position is None:
            return
        if stop is not None:
            position.stop_price = to_decimal(stop)
        if target is not None:
            position.take_profit = to_decimal(target)

    def recent_trades(self, n: int = 20) -> List[TradeRecord]:
        return self.trades[-n:]

    def trades_by_strategy(self) -> Dict[str, List[TradeRecord]]:
        grouped: Dict[str, List[TradeRecord]] = {}
        for trade in self.trades:
            grouped.setdefault(trade.strategy or "unknown", []).append(trade)
        return grouped

    def trades_by_regime(self) -> Dict[str, List[TradeRecord]]:
        grouped: Dict[str, List[TradeRecord]] = {}
        for trade in self.trades:
            grouped.setdefault(trade.regime.value, []).append(trade)
        return grouped

    def summary(self) -> str:
        s = self.stats
        return (
            f"equity={self.equity():.2f} {self.quote_currency} "
            f"cash={self.cash:.2f} open={self.open_count} "
            f"trades={s.total_trades} win={s.win_rate:.1%} "
            f"pnl={s.realized_pnl:+.2f} fees={s.fees_paid:.2f} "
            f"maxDD={s.max_drawdown:.1%}"
        )


__all__ = ["PortfolioStats", "PositionBook"]
