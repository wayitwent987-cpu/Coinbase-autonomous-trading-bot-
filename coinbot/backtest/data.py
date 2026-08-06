"""A broker that replays historical candles.

The point of this class is that the backtester can drive the *real*
:class:`~coinbot.engine.bot.TradingBot` loop. Backtesters written as a separate
simplified loop always drift from the live code, and the differences surface as
live behaviour nobody predicted. Here the only thing that changes between a
backtest and a live run is which Broker is plugged in.

Two rules keep the replay honest:

* **No lookahead.** Only bars at or before the cursor are ever served, so
  indicators see exactly what they would have seen at that moment.
* **Fills happen at the next bar's open**, not the close of the bar that
  produced the signal. You cannot decide on a close and also transact at it.
  Using the next open is what a real bot placing an order on bar close gets.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..execution.broker import Broker
from ..execution.paper import PaperBroker
from ..models import (
    AccountSnapshot,
    Candle,
    Fill,
    Granularity,
    Order,
    OrderRequest,
    Product,
    Quote,
)
from ..util.numeric import to_decimal


class HistoricalBroker(Broker):
    """Serves recorded candles up to a moving cursor; delegates orders to paper."""

    mode = "backtest"

    def __init__(
        self,
        candles: Dict[str, List[Candle]],
        *,
        paper: PaperBroker,
        htf_candles: Optional[Dict[str, List[Candle]]] = None,
        products: Optional[Dict[str, Product]] = None,
        start_index: int = 0,
    ) -> None:
        if not candles:
            raise ValueError("HistoricalBroker needs at least one product's candles")

        self.candles = {pid: sorted(bars, key=lambda c: c.start) for pid, bars in candles.items()}
        self.htf_candles = {
            pid: sorted(bars, key=lambda c: c.start)
            for pid, bars in (htf_candles or {}).items()
        }
        self.paper = paper
        self._products: Dict[str, Product] = dict(products or {})

        self.length = min(len(bars) for bars in self.candles.values())
        if self.length < 2:
            raise ValueError("need at least 2 bars per product to replay")
        self.cursor = max(0, min(start_index, self.length - 1))

        # Fills must be stamped with *simulated* time. On wall-clock time an
        # entire backtest happens within a second, and every reported holding
        # period collapses to zero.
        self.paper.clock = lambda: float(self.current_time)

        for product in self._products.values():
            self.paper.register_product(product)
        self._sync_marks()

    # ------------------------------------------------------------------ #
    # Replay control
    # ------------------------------------------------------------------ #

    def advance(self) -> bool:
        """Step forward one bar. Returns False at the end of the data."""
        if self.cursor >= self.length - 1:
            return False
        self.cursor += 1
        # Resting limit orders get a chance to fill against the bar that just
        # completed, before any new decision is made on it.
        for pid, bars in self.candles.items():
            if self.cursor < len(bars):
                self.paper.on_bar(pid, bars[self.cursor])
        self._sync_marks()
        return True

    def _sync_marks(self) -> None:
        """Point the paper broker at the price an order would actually get.

        The bot decides on the bar at the cursor, so the earliest it could
        transact is the following bar's open. Marking there removes the
        fill-at-the-signal-close lookahead that flatters most backtests.
        """
        for pid, bars in self.candles.items():
            nxt = self.cursor + 1
            price = bars[nxt].open if nxt < len(bars) else bars[self.cursor].close
            self.paper.set_mark(pid, to_decimal(price))

    @property
    def current_time(self) -> int:
        first = next(iter(self.candles.values()))
        return first[min(self.cursor, len(first) - 1)].start

    @property
    def progress(self) -> float:
        return (self.cursor + 1) / self.length if self.length else 1.0

    def remaining(self) -> int:
        return max(0, self.length - 1 - self.cursor)

    # ------------------------------------------------------------------ #
    # Broker interface
    # ------------------------------------------------------------------ #

    def get_product(self, product_id: str) -> Product:
        if product_id in self._products:
            return self._products[product_id]
        product = self.paper.get_product(product_id)
        self._products[product_id] = product
        return product

    def get_candles(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        bars: int,
        end: Optional[int] = None,
    ) -> List[Candle]:
        """History up to the cursor — never beyond it."""
        cutoff = self.current_time

        source = self.candles.get(product_id, [])
        if product_id in self.htf_candles and self.htf_candles[product_id]:
            htf = self.htf_candles[product_id]
            # Pick the series whose spacing matches the requested granularity.
            if len(htf) > 1 and (htf[1].start - htf[0].start) == granularity.seconds:
                source = htf

        visible = [c for c in source if c.start <= cutoff]
        return visible[-bars:] if bars > 0 else visible

    def get_quotes(self, product_ids: Sequence[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for pid in product_ids:
            mark = self.paper.mark(pid)
            if mark is None or mark <= 0:
                continue
            half = mark * Decimal("0.0002")  # a plausible 4 bps spread
            out[pid] = Quote(product_id=pid, bid=mark - half, ask=mark + half)
        return out

    def get_account(self, quote_currency: str = "USD") -> AccountSnapshot:
        return self.paper.get_account(quote_currency)

    def place_order(self, request: OrderRequest) -> Order:
        return self.paper.place_order(request)

    def cancel_order(self, order_id: str) -> bool:
        return self.paper.cancel_order(order_id)

    def get_order(self, order_id: str) -> Order:
        return self.paper.get_order(order_id)

    def get_fills(self, *, order_id: Optional[str] = None) -> List[Fill]:
        return self.paper.get_fills(order_id=order_id)

    def list_open_orders(self, product_id: Optional[str] = None) -> List[Order]:
        return self.paper.list_open_orders(product_id)

    @property
    def is_live(self) -> bool:
        return False


__all__ = ["HistoricalBroker"]
