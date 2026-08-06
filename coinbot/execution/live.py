"""Live broker — real orders against Coinbase Advanced Trade."""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..config import ApiConfig, TradingConfig
from ..exchange.rest import CoinbaseClient
from ..logging_setup import get_logger
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
from .broker import Broker

log = get_logger("broker.live")


class LiveBroker(Broker):
    """Routes every call to the real exchange.

    Deliberately thin: it holds no simulated state, so anything that looks
    wrong here is wrong on the exchange too, not an artifact of local
    bookkeeping.
    """

    mode = "live"

    def __init__(
        self,
        client: Optional[CoinbaseClient] = None,
        api_cfg: Optional[ApiConfig] = None,
        trading_cfg: Optional[TradingConfig] = None,
    ) -> None:
        self.client = client or CoinbaseClient(api_cfg)
        self.trading_cfg = trading_cfg or TradingConfig()
        self._fee_rates: Optional[Dict[str, float]] = None

    # -- Reference data ------------------------------------------------- #

    def get_product(self, product_id: str) -> Product:
        return self.client.get_product(product_id)

    def get_candles(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        bars: int,
        end: Optional[int] = None,
    ) -> List[Candle]:
        return self.client.get_candles_paged(product_id, granularity, bars=bars, end=end)

    def get_quotes(self, product_ids: Sequence[str]) -> Dict[str, Quote]:
        return self.client.get_quotes(product_ids)

    # -- Account -------------------------------------------------------- #

    def get_account(self, quote_currency: str = "USD") -> AccountSnapshot:
        balances = self.client.get_accounts()
        quote = balances.get(quote_currency, {})
        return AccountSnapshot(
            quote_currency=quote_currency,
            cash=quote.get("available", Decimal(0)),
            holds=quote.get("hold", Decimal(0)),
            balances={cur: vals.get("available", Decimal(0)) for cur, vals in balances.items()},
        )

    def fee_rates(self) -> Dict[str, float]:
        """Actual fee tier, cached — it changes with 30-day volume, not per bar."""
        if self._fee_rates is None:
            try:
                self._fee_rates = self.client.get_fee_rates()
                log.info(
                    "fee tier: maker %.3f%% taker %.3f%%",
                    self._fee_rates["maker"] * 100, self._fee_rates["taker"] * 100,
                )
            except Exception as exc:
                log.warning("could not read fee tier (%s); using conservative defaults", exc)
                self._fee_rates = {"maker": 0.004, "taker": 0.006}
        return self._fee_rates

    # -- Orders --------------------------------------------------------- #

    def place_order(self, request: OrderRequest) -> Order:
        log.info(
            "LIVE ORDER %s %s %s @ %s [%s] %s",
            request.side.value, request.base_size or request.quote_size,
            request.product_id, request.limit_price or "market",
            request.order_type.value, request.reason,
        )
        order = self.client.place_order(request)
        log.info("order accepted id=%s coid=%s", order.order_id, order.client_order_id)
        return order

    def cancel_order(self, order_id: str) -> bool:
        ok = self.client.cancel_order(order_id)
        log.info("cancel %s -> %s", order_id, "ok" if ok else "failed")
        return ok

    def get_order(self, order_id: str) -> Order:
        return self.client.get_order(order_id)

    def get_fills(self, *, order_id: Optional[str] = None) -> List[Fill]:
        return self.client.get_fills(order_id=order_id)

    def list_open_orders(self, product_id: Optional[str] = None) -> List[Order]:
        return self.client.list_open_orders(product_id)

    # -- Lifecycle ------------------------------------------------------ #

    @property
    def is_live(self) -> bool:
        return True

    def close(self) -> None:
        self.client.close()


__all__ = ["LiveBroker"]
