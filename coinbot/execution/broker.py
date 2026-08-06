"""The broker interface every execution path implements.

One interface, three implementations (live, paper, backtest) means the trading
engine has no idea whether it is trading real money. That is the only way to be
confident a backtest and a live run take the same decisions — the difference is
confined to this seam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

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


class Broker(ABC):
    """Everything the engine needs from an execution venue."""

    #: Human-readable mode, surfaced in logs and the status line.
    mode: str = "abstract"

    # -- Reference data ------------------------------------------------- #

    @abstractmethod
    def get_product(self, product_id: str) -> Product:
        """Product metadata, including increments and minimum sizes."""

    @abstractmethod
    def get_candles(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        bars: int,
        end: Optional[int] = None,
    ) -> List[Candle]:
        """Historical candles in ascending time order."""

    @abstractmethod
    def get_quotes(self, product_ids: Sequence[str]) -> Dict[str, Quote]:
        """Top-of-book for each product."""

    # -- Account -------------------------------------------------------- #

    @abstractmethod
    def get_account(self, quote_currency: str = "USD") -> AccountSnapshot:
        """Current balances."""

    # -- Orders --------------------------------------------------------- #

    @abstractmethod
    def place_order(self, request: OrderRequest) -> Order:
        """Submit an order."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel one order. Returns True if the venue accepted the cancel."""

    @abstractmethod
    def get_order(self, order_id: str) -> Order:
        """Current state of a submitted order."""

    @abstractmethod
    def get_fills(self, *, order_id: Optional[str] = None) -> List[Fill]:
        """Fills, optionally narrowed to one order."""

    def list_open_orders(self, product_id: Optional[str] = None) -> List[Order]:
        """Working orders. Default is "none", which suits stateless brokers."""
        return []

    # -- Lifecycle ------------------------------------------------------ #

    def sync(self) -> None:
        """Hook called once per loop for brokers that simulate time."""

    def close(self) -> None:
        """Release any held resources."""

    @property
    def is_live(self) -> bool:
        return False


__all__ = ["Broker"]
