"""Core domain types shared by every layer of the bot.

Everything here is a plain dataclass or enum: no I/O, no exchange coupling.
Prices and sizes that touch the exchange are ``Decimal``; values that only feed
indicator math are ``float`` (speed matters more than exactness there).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        )


class SignalAction(str, Enum):
    """What a strategy wants to happen, independent of sizing."""

    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Regime(str, Enum):
    """Coarse market state used to route between strategies."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE_CHOP = "VOLATILE_CHOP"
    CRASH = "CRASH"
    SQUEEZE = "SQUEEZE"
    UNKNOWN = "UNKNOWN"


class Granularity(str, Enum):
    """Coinbase Advanced Trade candle granularities and their length."""

    ONE_MINUTE = "ONE_MINUTE"
    FIVE_MINUTE = "FIVE_MINUTE"
    FIFTEEN_MINUTE = "FIFTEEN_MINUTE"
    THIRTY_MINUTE = "THIRTY_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    TWO_HOUR = "TWO_HOUR"
    SIX_HOUR = "SIX_HOUR"
    ONE_DAY = "ONE_DAY"

    @property
    def seconds(self) -> int:
        return _GRANULARITY_SECONDS[self]

    @classmethod
    def from_seconds(cls, seconds: int) -> "Granularity":
        for gran, secs in _GRANULARITY_SECONDS.items():
            if secs == seconds:
                return gran
        raise ValueError(f"no Coinbase granularity of {seconds}s")


_GRANULARITY_SECONDS: Dict[Granularity, int] = {
    Granularity.ONE_MINUTE: 60,
    Granularity.FIVE_MINUTE: 300,
    Granularity.FIFTEEN_MINUTE: 900,
    Granularity.THIRTY_MINUTE: 1800,
    Granularity.ONE_HOUR: 3600,
    Granularity.TWO_HOUR: 7200,
    Granularity.SIX_HOUR: 21600,
    Granularity.ONE_DAY: 86400,
}


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV bar. ``start`` is the bar's opening unix time (seconds)."""

    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @classmethod
    def from_coinbase(cls, raw: Dict[str, Any]) -> "Candle":
        return cls(
            start=int(raw["start"]),
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=float(raw.get("volume", 0.0) or 0.0),
        )


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book snapshot."""

    product_id: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return 0.0
        return float(self.spread / mid) * 10_000.0


@dataclass(frozen=True, slots=True)
class Product:
    """Trading-pair metadata, including the increments orders must respect."""

    product_id: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal
    quote_increment: Decimal
    price_increment: Decimal
    min_base_size: Decimal = Decimal(0)
    min_quote_size: Decimal = Decimal(0)
    max_base_size: Optional[Decimal] = None
    trading_disabled: bool = False
    limit_only: bool = False
    post_only: bool = False
    status: str = "online"

    @property
    def tradable(self) -> bool:
        return not self.trading_disabled and self.status.lower() == "online"

    @classmethod
    def from_coinbase(cls, raw: Dict[str, Any]) -> "Product":
        def dec(key: str, default: str = "0") -> Decimal:
            value = raw.get(key)
            if value in (None, ""):
                return Decimal(default)
            return Decimal(str(value))

        price_inc = dec("price_increment", "0")
        if price_inc == 0:
            price_inc = dec("quote_increment", "0.01")
        max_base = raw.get("base_max_size")
        return cls(
            product_id=raw["product_id"],
            base_currency=raw.get("base_currency_id") or raw.get("base_currency", ""),
            quote_currency=raw.get("quote_currency_id") or raw.get("quote_currency", ""),
            base_increment=dec("base_increment", "0.00000001"),
            quote_increment=dec("quote_increment", "0.01"),
            price_increment=price_inc,
            min_base_size=dec("base_min_size", "0"),
            min_quote_size=dec("quote_min_size", "0"),
            max_base_size=Decimal(str(max_base)) if max_base else None,
            trading_disabled=bool(raw.get("trading_disabled", False)),
            limit_only=bool(raw.get("limit_only", False)),
            post_only=bool(raw.get("post_only", False)),
            status=str(raw.get("status", "online")),
        )


# --------------------------------------------------------------------------- #
# Orders and fills
# --------------------------------------------------------------------------- #


def new_client_order_id(prefix: str = "cbb") -> str:
    """Idempotency key. Coinbase rejects duplicates, which is exactly the
    protection we want against a retry double-submitting an order."""
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


@dataclass
class OrderRequest:
    product_id: str
    side: Side
    order_type: OrderType
    base_size: Optional[Decimal] = None
    quote_size: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    stop_direction: Optional[str] = None  # STOP_DIRECTION_STOP_UP / _DOWN
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str = field(default_factory=new_client_order_id)
    reason: str = ""

    def validate(self) -> None:
        if self.base_size is None and self.quote_size is None:
            raise ValueError("order needs base_size or quote_size")
        if self.base_size is not None and self.base_size <= 0:
            raise ValueError(f"base_size must be positive, got {self.base_size}")
        if self.quote_size is not None and self.quote_size <= 0:
            raise ValueError(f"quote_size must be positive, got {self.quote_size}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order needs limit_price")
        if self.order_type is OrderType.STOP_LIMIT:
            if self.limit_price is None or self.stop_price is None:
                raise ValueError("stop-limit order needs both limit_price and stop_price")


@dataclass
class Order:
    order_id: str
    client_order_id: str
    product_id: str
    side: Side
    order_type: OrderType
    status: OrderStatus = OrderStatus.PENDING
    limit_price: Optional[Decimal] = None
    base_size: Optional[Decimal] = None
    filled_size: Decimal = Decimal(0)
    avg_fill_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    reject_reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def notional(self) -> Decimal:
        return self.filled_size * self.avg_fill_price


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    product_id: str
    side: Side
    size: Decimal
    price: Decimal
    fee: Decimal
    ts: float = field(default_factory=time.time)
    trade_id: str = ""

    @property
    def notional(self) -> Decimal:
        return self.size * self.price


# --------------------------------------------------------------------------- #
# Positions and signals
# --------------------------------------------------------------------------- #


@dataclass
class Position:
    """A net position in one product. Spot is long-only; ``size`` may go
    negative only when the venue and config permit shorting."""

    product_id: str
    size: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    opened_at: Optional[float] = None
    last_price: Decimal = Decimal(0)
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    trail_anchor: Optional[Decimal] = None
    strategy: str = ""
    regime_at_entry: Regime = Regime.UNKNOWN
    bars_held: int = 0

    @property
    def is_open(self) -> bool:
        return self.size != 0

    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def is_short(self) -> bool:
        return self.size < 0

    @property
    def side(self) -> Optional[Side]:
        if self.size > 0:
            return Side.BUY
        if self.size < 0:
            return Side.SELL
        return None

    @property
    def notional(self) -> Decimal:
        price = self.last_price or self.entry_price
        return abs(self.size) * price

    @property
    def unrealized_pnl(self) -> Decimal:
        if not self.is_open or not self.last_price:
            return Decimal(0)
        return (self.last_price - self.entry_price) * self.size

    @property
    def unrealized_pnl_pct(self) -> float:
        if not self.is_open or not self.entry_price:
            return 0.0
        cost = abs(self.size) * self.entry_price
        if cost == 0:
            return 0.0
        return float(self.unrealized_pnl / cost)


@dataclass
class Signal:
    """A strategy's opinion for one product on one bar.

    ``strength`` in [0, 1] scales the risk budget the sizer allocates; a
    strategy that is only mildly confident should say so rather than always
    demanding a full-size position.
    """

    product_id: str
    action: SignalAction
    strength: float = 0.0
    strategy: str = ""
    reason: str = ""
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.strength = max(0.0, min(1.0, float(self.strength)))

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def direction(self) -> int:
        if self.action is SignalAction.ENTER_LONG:
            return 1
        if self.action is SignalAction.ENTER_SHORT:
            return -1
        return 0

    @classmethod
    def hold(cls, product_id: str, strategy: str = "", reason: str = "") -> "Signal":
        return cls(
            product_id=product_id,
            action=SignalAction.HOLD,
            strategy=strategy,
            reason=reason,
        )


@dataclass
class RegimeState:
    """Regime detector output, with the diagnostics that produced it."""

    regime: Regime = Regime.UNKNOWN
    confidence: float = 0.0
    trend_score: float = 0.0
    volatility_pct: float = 0.0
    adx: float = 0.0
    scores: Dict[str, float] = field(default_factory=dict)
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - display only
        return (
            f"{self.regime.value}(conf={self.confidence:.2f}, "
            f"trend={self.trend_score:+.2f}, vol={self.volatility_pct:.2%}, "
            f"adx={self.adx:.1f})"
        )


@dataclass
class AccountSnapshot:
    """Balances plus derived equity, refreshed every loop."""

    quote_currency: str = "USD"
    cash: Decimal = Decimal(0)
    holds: Decimal = Decimal(0)
    positions_value: Decimal = Decimal(0)
    balances: Dict[str, Decimal] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def equity(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def available_cash(self) -> Decimal:
        return max(Decimal(0), self.cash - self.holds)


@dataclass
class TradeRecord:
    """A completed round trip, for the performance report."""

    product_id: str
    strategy: str
    side: Side
    size: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    fees: Decimal
    opened_at: float
    closed_at: float
    regime: Regime = Regime.UNKNOWN
    exit_reason: str = ""

    @property
    def net_pnl(self) -> Decimal:
        """PnL after both sides' fees. ``pnl`` itself is gross."""
        return self.pnl - self.fees

    @property
    def return_pct(self) -> float:
        cost = abs(self.size) * self.entry_price
        if cost == 0:
            return 0.0
        return float(self.pnl / cost)

    @property
    def net_return_pct(self) -> float:
        cost = abs(self.size) * self.entry_price
        if cost == 0:
            return 0.0
        return float(self.net_pnl / cost)

    @property
    def duration_s(self) -> float:
        return self.closed_at - self.opened_at

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


__all__ = [
    "AccountSnapshot",
    "Candle",
    "Fill",
    "Granularity",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "Product",
    "Quote",
    "Regime",
    "RegimeState",
    "Side",
    "Signal",
    "SignalAction",
    "TimeInForce",
    "TradeRecord",
    "new_client_order_id",
]
