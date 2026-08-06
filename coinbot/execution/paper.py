"""Paper broker — a fill simulator with real market data.

Used for both dry-run live trading and backtesting. Two choices here decide
whether paper results mean anything:

* **Limit orders do not fill just because price touched the limit.** Price has
  to trade *through* it, because at the touch you are last in the queue. This
  single rule is the difference between a believable backtest and a fantasy.
* **Costs are always charged.** Fees on every fill and slippage on every
  marketable order. A simulator that trades for free will happily "prove" a
  strategy that loses money the moment it meets a real fee schedule.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence

from ..config import ExecutionConfig
from ..logging_setup import get_logger
from ..models import (
    AccountSnapshot,
    Candle,
    Fill,
    Granularity,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Quote,
    Side,
)
from ..util.numeric import from_bps, round_size, to_decimal
from ..util.timing import now_ts
from .broker import Broker

log = get_logger("broker.paper")


class PaperBroker(Broker):
    """Simulates an account and an order book against supplied prices."""

    mode = "paper"

    def __init__(
        self,
        *,
        starting_cash: Decimal = Decimal("10000"),
        quote_currency: str = "USD",
        data_source: Optional[Broker] = None,
        exec_cfg: Optional[ExecutionConfig] = None,
        products: Optional[Dict[str, Product]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.quote_currency = quote_currency
        self.cfg = exec_cfg or ExecutionConfig()
        self.data_source = data_source
        self.clock = clock or now_ts

        self.balances: Dict[str, Decimal] = {quote_currency: to_decimal(starting_cash)}
        self.starting_cash = to_decimal(starting_cash)

        self._products: Dict[str, Product] = dict(products or {})
        self._marks: Dict[str, Decimal] = {}
        self._orders: Dict[str, Order] = {}
        self._requests: Dict[str, OrderRequest] = {}
        self._fills: List[Fill] = []
        self._sequence = 0

    # ------------------------------------------------------------------ #
    # Market data (delegated, or injected by the backtester)
    # ------------------------------------------------------------------ #

    def get_product(self, product_id: str) -> Product:
        if product_id in self._products:
            return self._products[product_id]
        if self.data_source is not None:
            product = self.data_source.get_product(product_id)
            self._products[product_id] = product
            return product
        # A permissive default keeps unit tests from needing a live connection.
        base, _, quote = product_id.partition("-")
        product = Product(
            product_id=product_id,
            base_currency=base,
            quote_currency=quote or self.quote_currency,
            base_increment=Decimal("0.00000001"),
            quote_increment=Decimal("0.01"),
            price_increment=Decimal("0.01"),
        )
        self._products[product_id] = product
        return product

    def register_product(self, product: Product) -> None:
        self._products[product.product_id] = product

    def get_candles(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        bars: int,
        end: Optional[int] = None,
    ) -> List[Candle]:
        if self.data_source is None:
            return []
        return self.data_source.get_candles(product_id, granularity, bars=bars, end=end)

    def get_quotes(self, product_ids: Sequence[str]) -> Dict[str, Quote]:
        if self.data_source is not None:
            quotes = self.data_source.get_quotes(product_ids)
            for pid, quote in quotes.items():
                self._marks[pid] = quote.mid
            return quotes
        # Synthesize a book around the last mark with a token spread.
        out: Dict[str, Quote] = {}
        for pid in product_ids:
            mark = self._marks.get(pid)
            if mark is None:
                continue
            half = mark * Decimal("0.0001")
            out[pid] = Quote(product_id=pid, bid=mark - half, ask=mark + half)
        return out

    # ------------------------------------------------------------------ #
    # Price feed
    # ------------------------------------------------------------------ #

    def set_mark(self, product_id: str, price: Decimal) -> None:
        self._marks[product_id] = to_decimal(price)

    def mark(self, product_id: str) -> Optional[Decimal]:
        return self._marks.get(product_id)

    def on_bar(self, product_id: str, candle: Candle) -> List[Fill]:
        """Advance simulated time by one bar and fill any crossed orders."""
        self.set_mark(product_id, to_decimal(candle.close))
        return self._process_resting_orders(product_id, candle)

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_account(self, quote_currency: str = "USD") -> AccountSnapshot:
        currency = quote_currency or self.quote_currency
        cash = self.balances.get(currency, Decimal(0))
        positions_value = Decimal(0)
        for asset, amount in self.balances.items():
            if asset == currency or amount == 0:
                continue
            mark = self._marks.get(f"{asset}-{currency}")
            if mark:
                positions_value += amount * mark
        return AccountSnapshot(
            quote_currency=currency,
            cash=cash,
            holds=self._reserved(currency),
            positions_value=positions_value,
            balances=dict(self.balances),
            ts=self.clock(),
        )

    def _reserved(self, currency: str) -> Decimal:
        """Cash tied up by resting buy orders."""
        total = Decimal(0)
        for order in self._orders.values():
            if order.status not in (OrderStatus.OPEN, OrderStatus.PENDING):
                continue
            if order.side is Side.BUY and order.limit_price and order.base_size:
                remaining = order.base_size - order.filled_size
                total += remaining * order.limit_price
        return total

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(self, request: OrderRequest) -> Order:
        request.validate()
        product = self.get_product(request.product_id)
        mark = self._marks.get(request.product_id)
        if mark is None:
            raise ValueError(
                f"paper broker has no price for {request.product_id}; "
                "feed it a bar or a quote before trading"
            )

        self._sequence += 1
        order_id = f"paper-{self._sequence:08d}"

        base_size = request.base_size
        if base_size is None and request.quote_size is not None:
            base_size = round_size(request.quote_size / mark, product.base_increment)

        order = Order(
            order_id=order_id,
            client_order_id=request.client_order_id,
            product_id=request.product_id,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.PENDING,
            limit_price=request.limit_price,
            base_size=base_size,
            created_at=self.clock(),
        )

        if base_size is None or base_size <= 0:
            order.status = OrderStatus.FAILED
            order.reject_reason = "size rounds to zero"
            self._orders[order_id] = order
            return order

        self._orders[order_id] = order
        self._requests[order_id] = request

        if request.order_type is OrderType.MARKET:
            self._fill(order, self._marketable_price(request.side, mark), reason="market")
        elif request.order_type is OrderType.LIMIT:
            crosses = (
                request.side is Side.BUY and request.limit_price >= mark
            ) or (
                request.side is Side.SELL and request.limit_price <= mark
            )
            if crosses and not request.post_only:
                # Marketable limit: fills at the better of limit and mark.
                price = min(request.limit_price, mark) if request.side is Side.BUY else max(
                    request.limit_price, mark
                )
                self._fill(order, price, reason="marketable limit", maker=False)
            elif crosses and request.post_only:
                order.status = OrderStatus.CANCELLED
                order.reject_reason = "post-only order would have crossed the spread"
            else:
                order.status = OrderStatus.OPEN
        else:
            order.status = OrderStatus.OPEN  # stop-limit rests until triggered

        return order

    def _marketable_price(self, side: Side, mark: Decimal) -> Decimal:
        """Apply slippage against us — the only honest direction."""
        slip = to_decimal(from_bps(self.cfg.slippage_bps))
        return mark * (Decimal(1) + slip) if side is Side.BUY else mark * (Decimal(1) - slip)

    def _process_resting_orders(self, product_id: str, candle: Candle) -> List[Fill]:
        """Fill working orders that this bar traded through."""
        filled: List[Fill] = []
        for order in list(self._orders.values()):
            if order.product_id != product_id or order.status is not OrderStatus.OPEN:
                continue
            request = self._requests.get(order.order_id)
            if request is None or order.limit_price is None:
                continue

            high = to_decimal(candle.high)
            low = to_decimal(candle.low)

            # Strictly through the limit, not merely touching it: at a touch
            # there is a queue ahead of us and no reason to assume we filled.
            crossed = (
                low < order.limit_price
                if order.side is Side.BUY
                else high > order.limit_price
            )
            if not crossed:
                continue

            fill = self._fill(order, order.limit_price, reason="limit crossed", maker=True)
            if fill is not None:
                filled.append(fill)
        return filled

    def _fill(
        self,
        order: Order,
        price: Decimal,
        *,
        reason: str,
        maker: bool = False,
    ) -> Optional[Fill]:
        """Settle a fill: move balances, charge the fee, record it."""
        size = (order.base_size or Decimal(0)) - order.filled_size
        if size <= 0:
            return None

        product = self.get_product(order.product_id)
        base_ccy = product.base_currency
        quote_ccy = product.quote_currency or self.quote_currency
        notional = size * price
        fee_bps = self.cfg.maker_fee_bps if maker else self.cfg.taker_fee_bps
        fee = notional * to_decimal(from_bps(fee_bps))

        if order.side is Side.BUY:
            cost = notional + fee
            available = self.balances.get(quote_ccy, Decimal(0))
            if cost > available:
                order.status = OrderStatus.FAILED
                order.reject_reason = (
                    f"insufficient {quote_ccy}: need {cost:.2f}, have {available:.2f}"
                )
                log.warning("paper order %s rejected: %s", order.order_id, order.reject_reason)
                return None
            self.balances[quote_ccy] = available - cost
            self.balances[base_ccy] = self.balances.get(base_ccy, Decimal(0)) + size
        else:
            held = self.balances.get(base_ccy, Decimal(0))
            if size > held:
                order.status = OrderStatus.FAILED
                order.reject_reason = (
                    f"insufficient {base_ccy}: need {size}, have {held}"
                )
                log.warning("paper order %s rejected: %s", order.order_id, order.reject_reason)
                return None
            self.balances[base_ccy] = held - size
            self.balances[quote_ccy] = self.balances.get(quote_ccy, Decimal(0)) + notional - fee

        order.filled_size += size
        order.avg_fill_price = price
        order.fee += fee
        order.status = OrderStatus.FILLED
        order.updated_at = self.clock()

        fill = Fill(
            order_id=order.order_id,
            product_id=order.product_id,
            side=order.side,
            size=size,
            price=price,
            fee=fee,
            ts=self.clock(),
            trade_id=f"{order.order_id}-1",
        )
        self._fills.append(fill)
        log.debug(
            "paper fill %s %s %s @ %s fee=%.4f (%s)",
            order.side.value, size, order.product_id, price, fee, reason,
        )
        return fill

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.is_terminal:
            return False
        order.status = OrderStatus.CANCELLED
        order.updated_at = self.clock()
        return True

    def get_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown paper order {order_id}")
        return order

    def get_fills(self, *, order_id: Optional[str] = None) -> List[Fill]:
        if order_id is None:
            return list(self._fills)
        return [f for f in self._fills if f.order_id == order_id]

    def list_open_orders(self, product_id: Optional[str] = None) -> List[Order]:
        return [
            o for o in self._orders.values()
            if o.status is OrderStatus.OPEN
            and (product_id is None or o.product_id == product_id)
        ]

    def cancel_all(self, product_id: Optional[str] = None) -> int:
        count = 0
        for order in self.list_open_orders(product_id):
            if self.cancel_order(order.order_id):
                count += 1
        return count

    # ------------------------------------------------------------------ #

    def sync(self) -> None:
        """Refresh marks from the data source, if there is one."""
        if self.data_source is not None and self._marks:
            try:
                self.get_quotes(list(self._marks))
            except Exception as exc:  # pragma: no cover - network dependent
                log.debug("paper broker mark refresh failed: %s", exc)

    def reset(self, starting_cash: Optional[Decimal] = None) -> None:
        cash = to_decimal(starting_cash) if starting_cash is not None else self.starting_cash
        self.balances = {self.quote_currency: cash}
        self.starting_cash = cash
        self._orders.clear()
        self._requests.clear()
        self._fills.clear()
        self._sequence = 0

    @property
    def is_live(self) -> bool:
        return False


__all__ = ["PaperBroker"]
