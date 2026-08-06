"""Execution engine: turns a sized decision into orders, and chases fills.

Sitting between the risk manager and the broker, this layer owns the questions
that are neither strategy nor risk: limit or market, what price, how long to
wait, and when to give up and cross the spread.

The default is a passive limit just inside the touch, re-priced for a bounded
number of bars, then escalated to market. Always crossing the spread pays the
taker fee on every trade, which quietly eats most of the edge of a
higher-frequency strategy; never crossing means missing the moves worth
catching. Chasing then escalating splits the difference and, crucially, bounds
how long the bot can sit with an intended-but-absent position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..config import ExecutionConfig, RiskConfig
from ..logging_setup import get_logger
from ..models import (
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    Quote,
    Side,
    TimeInForce,
    new_client_order_id,
)
from ..util.numeric import bps, from_bps, round_price, round_size, to_decimal
from ..util.timing import now_ts
from .broker import Broker

log = get_logger("execution")


@dataclass
class WorkingOrder:
    """An order the engine is actively managing.

    Carries the *intent* behind the order, not just the order itself. A limit
    entry can rest for several bars, and when it finally fills the bot still
    has to know which strategy wanted it and what stop to attach — without
    that, an order that does not fill instantly loses its risk parameters.
    """

    order: Order
    intent: str                 # "entry" | "exit"
    strategy: str = ""
    reason: str = ""
    placed_at: float = field(default_factory=now_ts)
    bars_working: int = 0
    reprices: int = 0
    target_size: Decimal = Decimal(0)
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    regime: str = ""

    @property
    def age_s(self) -> float:
        return now_ts() - self.placed_at


@dataclass
class FillEvent:
    """A fill together with the intent that produced it."""

    fill: Fill
    intent: str = ""
    strategy: str = ""
    reason: str = ""
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    regime: str = ""

    @property
    def is_entry(self) -> bool:
        return self.intent == "entry"


@dataclass
class ExecutionResult:
    """What happened when the engine tried to act."""

    submitted: Optional[Order] = None
    fills: List[Fill] = field(default_factory=list)
    skipped: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.submitted is not None and not self.error

    @property
    def resting(self) -> bool:
        """True when the order is working and has not filled yet."""
        return self.submitted is not None and not self.submitted.is_terminal and not self.fills


class ExecutionEngine:
    """Places, prices, chases and reconciles orders."""

    def __init__(
        self,
        broker: Broker,
        exec_cfg: Optional[ExecutionConfig] = None,
        risk_cfg: Optional[RiskConfig] = None,
    ) -> None:
        self.broker = broker
        self.cfg = exec_cfg or ExecutionConfig()
        self.risk_cfg = risk_cfg or RiskConfig()
        self.working: Dict[str, WorkingOrder] = {}  # order_id -> WorkingOrder

    # ------------------------------------------------------------------ #
    # Order placement
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        *,
        product: Product,
        side: Side,
        size: Decimal,
        quote: Optional[Quote],
        reference_price: Decimal,
        strategy: str = "",
        reason: str = "",
        urgent: bool = False,
        stop_price: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
        regime: str = "",
    ) -> ExecutionResult:
        """Enter a position.

        ``stop_price`` / ``take_profit`` are carried with the order rather than
        applied by the caller, so a limit entry that rests for several bars
        still arrives with its risk parameters attached when it fills.
        """
        return self._submit(
            product=product, side=side, size=size, quote=quote,
            reference_price=reference_price, intent="entry",
            strategy=strategy, reason=reason, urgent=urgent,
            stop_price=stop_price, take_profit=take_profit, regime=regime,
        )

    def close_position(
        self,
        *,
        product: Product,
        side: Side,
        size: Decimal,
        quote: Optional[Quote],
        reference_price: Decimal,
        strategy: str = "",
        reason: str = "",
        urgent: bool = True,
        regime: str = "",
    ) -> ExecutionResult:
        """Exit a position. Urgent by default — getting out beats saving a fee."""
        return self._submit(
            product=product, side=side, size=size, quote=quote,
            reference_price=reference_price, intent="exit",
            strategy=strategy, reason=reason, urgent=urgent, regime=regime,
        )

    def _submit(
        self,
        *,
        product: Product,
        side: Side,
        size: Decimal,
        quote: Optional[Quote],
        reference_price: Decimal,
        intent: str,
        strategy: str,
        reason: str,
        urgent: bool,
        stop_price: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
        regime: str = "",
    ) -> ExecutionResult:
        size = round_size(size, product.base_increment)
        if size <= 0:
            return ExecutionResult(skipped="size rounds to zero")
        if not product.tradable:
            return ExecutionResult(skipped=f"{product.product_id} is not tradable")

        # A blown-out spread means the book is thin or the market is
        # dislocated. Entering into that is how you get a terrible fill;
        # exiting is urgent enough to accept it.
        if quote is not None and intent == "entry":
            spread = quote.spread_bps
            if spread > self.risk_cfg.max_spread_bps:
                return ExecutionResult(
                    skipped=f"spread {spread:.1f}bps exceeds {self.risk_cfg.max_spread_bps:.1f}bps"
                )

        # Some products are limit-only; the venue rejects market orders there
        # regardless of how urgently we want out.
        use_market = (urgent or self.cfg.order_type == "market") and not product.limit_only

        request = self._build_request(
            product=product, side=side, size=size, quote=quote,
            reference_price=reference_price, use_market=use_market, reason=reason,
        )

        try:
            order = self.broker.place_order(request)
        except Exception as exc:
            log.error("order failed for %s: %s", product.product_id, exc)
            return ExecutionResult(error=str(exc))

        if order.status is OrderStatus.FAILED:
            return ExecutionResult(submitted=order, error=order.reject_reason)

        if not order.is_terminal:
            self.working[order.order_id] = WorkingOrder(
                order=order, intent=intent, strategy=strategy,
                reason=reason, target_size=size,
                stop_price=stop_price, take_profit=take_profit, regime=regime,
            )

        fills = self._collect_fills(order)
        return ExecutionResult(submitted=order, fills=fills)

    def _build_request(
        self,
        *,
        product: Product,
        side: Side,
        size: Decimal,
        quote: Optional[Quote],
        reference_price: Decimal,
        use_market: bool,
        reason: str,
    ) -> OrderRequest:
        if use_market:
            return OrderRequest(
                product_id=product.product_id,
                side=side,
                order_type=OrderType.MARKET,
                base_size=size,
                time_in_force=TimeInForce.IOC,
                client_order_id=new_client_order_id(),
                reason=reason,
            )

        price = self._limit_price(side, quote, reference_price, product)
        return OrderRequest(
            product_id=product.product_id,
            side=side,
            order_type=OrderType.LIMIT,
            base_size=size,
            limit_price=price,
            time_in_force=TimeInForce.GTC,
            post_only=self.cfg.post_only or product.post_only,
            client_order_id=new_client_order_id(),
            reason=reason,
        )

    def _limit_price(
        self,
        side: Side,
        quote: Optional[Quote],
        reference_price: Decimal,
        product: Product,
    ) -> Decimal:
        """Price just inside the touch, offset by ``limit_offset_bps``."""
        offset = to_decimal(from_bps(self.cfg.limit_offset_bps))
        if quote is not None and quote.bid > 0 and quote.ask > 0:
            # Join the near side rather than crossing it.
            base = quote.bid if side is Side.BUY else quote.ask
        else:
            base = to_decimal(reference_price)

        raw = base * (Decimal(1) + offset) if side is Side.BUY else base * (Decimal(1) - offset)

        if quote is not None and self.cfg.post_only:
            # Never price through the far touch, or a post-only order dies.
            if side is Side.BUY:
                raw = min(raw, quote.ask * Decimal("0.9999"))
            else:
                raw = max(raw, quote.bid * Decimal("1.0001"))

        return round_price(raw, product.price_increment, side is Side.BUY)

    # ------------------------------------------------------------------ #
    # Managing working orders
    # ------------------------------------------------------------------ #

    def reconcile(self, quotes: Optional[Dict[str, Quote]] = None) -> List[FillEvent]:
        """Poll working orders: collect fills, re-price or escalate stragglers.

        Call once per loop. Returns every new fill, each paired with the intent
        that produced it so the caller can attribute and configure the position.
        """
        collected: List[FillEvent] = []
        for order_id, working in list(self.working.items()):
            try:
                current = self.broker.get_order(order_id)
            except Exception as exc:
                log.warning("could not refresh order %s: %s", order_id, exc)
                continue

            working.order = current

            if current.is_terminal:
                if current.filled_size > 0:
                    collected.extend(
                        FillEvent(
                            fill=fill,
                            intent=working.intent,
                            strategy=working.strategy,
                            reason=working.reason,
                            stop_price=working.stop_price,
                            take_profit=working.take_profit,
                            regime=working.regime,
                        )
                        for fill in self._collect_fills(current)
                    )
                if current.status is OrderStatus.FILLED:
                    log.info(
                        "filled %s %s %s @ %s",
                        current.side.value, current.filled_size,
                        current.product_id, current.avg_fill_price,
                    )
                elif current.status is not OrderStatus.CANCELLED:
                    log.warning(
                        "order %s ended %s: %s",
                        order_id, current.status.value, current.reject_reason,
                    )
                del self.working[order_id]
                continue

            working.bars_working += 1
            quote = (quotes or {}).get(current.product_id)
            self._chase(order_id, working, quote)

        return collected

    def _chase(self, order_id: str, working: WorkingOrder, quote: Optional[Quote]) -> None:
        """Re-price a stale limit, then escalate to market once patience runs out."""
        stale_by_age = working.age_s > self.cfg.cancel_stale_after_s
        stale_by_bars = working.bars_working > self.cfg.chase_limit_bars
        if not (stale_by_age or stale_by_bars):
            return

        order = working.order
        remaining = (order.base_size or Decimal(0)) - order.filled_size
        if remaining <= 0:
            return

        try:
            self.broker.cancel_order(order_id)
        except Exception as exc:
            log.warning("cancel failed for %s: %s", order_id, exc)
            return
        self.working.pop(order_id, None)

        product = self.broker.get_product(order.product_id)
        escalate = working.reprices >= self.cfg.max_order_retries or working.intent == "exit"

        if escalate:
            log.info(
                "escalating %s %s to market after %d reprices",
                order.side.value, order.product_id, working.reprices,
            )
            result = self._submit(
                product=product, side=order.side, size=remaining, quote=quote,
                reference_price=order.limit_price or (quote.mid if quote else Decimal(0)),
                intent=working.intent, strategy=working.strategy,
                reason=f"{working.reason} (escalated)", urgent=True,
                stop_price=working.stop_price, take_profit=working.take_profit,
                regime=working.regime,
            )
        else:
            reference = quote.mid if quote else (order.limit_price or Decimal(0))
            if reference <= 0:
                return
            result = self._submit(
                product=product, side=order.side, size=remaining, quote=quote,
                reference_price=reference, intent=working.intent,
                strategy=working.strategy, reason=f"{working.reason} (reprice)",
                urgent=False,
                stop_price=working.stop_price, take_profit=working.take_profit,
                regime=working.regime,
            )

        if result.submitted is not None and not result.submitted.is_terminal:
            new_working = self.working.get(result.submitted.order_id)
            if new_working is not None:
                new_working.reprices = working.reprices + 1

    def _collect_fills(self, order: Order) -> List[Fill]:
        """Fills for an order, falling back to synthesizing one from totals."""
        try:
            fills = self.broker.get_fills(order_id=order.order_id)
            if fills:
                return fills
        except Exception as exc:
            log.debug("fill lookup failed for %s: %s", order.order_id, exc)

        if order.filled_size > 0 and order.avg_fill_price > 0:
            return [
                Fill(
                    order_id=order.order_id,
                    product_id=order.product_id,
                    side=order.side,
                    size=order.filled_size,
                    price=order.avg_fill_price,
                    fee=order.fee,
                    ts=order.updated_at,
                )
            ]
        return []

    # ------------------------------------------------------------------ #

    def cancel_all(self, product_id: Optional[str] = None) -> int:
        """Cancel everything working. Used on shutdown and on a risk halt."""
        count = 0
        for order_id, working in list(self.working.items()):
            if product_id and working.order.product_id != product_id:
                continue
            try:
                if self.broker.cancel_order(order_id):
                    count += 1
            except Exception as exc:
                log.warning("cancel failed for %s: %s", order_id, exc)
            self.working.pop(order_id, None)
        return count

    def has_working(self, product_id: str) -> bool:
        return any(w.order.product_id == product_id for w in self.working.values())

    def working_count(self) -> int:
        return len(self.working)

    def estimate_slippage_bps(self, expected: Decimal, actual: Decimal, side: Side) -> float:
        """Realized slippage in basis points, positive when it cost us."""
        if expected <= 0:
            return 0.0
        diff = (actual - expected) if side is Side.BUY else (expected - actual)
        return bps(float(diff / expected))


__all__ = ["ExecutionEngine", "ExecutionResult", "FillEvent", "WorkingOrder"]
