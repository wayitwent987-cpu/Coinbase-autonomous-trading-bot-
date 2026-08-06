"""Coinbase Advanced Trade REST client.

Covers exactly what the bot needs: product metadata, candles, balances, and
the order lifecycle. Every call goes through :meth:`CoinbaseClient._request`,
which owns rate limiting, retry-with-backoff on transient failures, and error
classification, so no endpoint method has to think about any of that.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from ..config import ApiConfig
from ..logging_setup import get_logger
from ..models import (
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
from ..util.numeric import to_decimal
from ..util.timing import RateLimiter, floor_to_interval, now_ts, parse_iso
from .auth import CoinbaseAuth
from .errors import (
    ExchangeError,
    ProductNotFoundError,
    RateLimitError,
    RetryableError,
    classify,
)

log = get_logger("coinbase")

#: Coinbase caps a single candles request at 350 bars.
MAX_CANDLES_PER_REQUEST = 300

_STATUS_MAP = {
    "PENDING": OrderStatus.PENDING,
    "OPEN": OrderStatus.OPEN,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.EXPIRED,
    "FAILED": OrderStatus.FAILED,
    "UNKNOWN_ORDER_STATUS": OrderStatus.UNKNOWN,
    "QUEUED": OrderStatus.PENDING,
}


class CoinbaseClient:
    """Thin, typed wrapper over the Advanced Trade REST API."""

    def __init__(self, cfg: Optional[ApiConfig] = None, *, session: Any = None) -> None:
        self.cfg = cfg or ApiConfig()
        if requests is None and session is None:  # pragma: no cover
            raise ExchangeError("the 'requests' package is required: pip install requests")
        self.session = session or requests.Session()
        self.auth: Optional[CoinbaseAuth] = None
        if self.cfg.has_credentials:
            self.auth = CoinbaseAuth(
                self.cfg.api_key, self.cfg.api_secret, ttl_seconds=self.cfg.jwt_ttl_s
            )
        self._limiter = RateLimiter(self.cfg.rate_limit_per_sec)
        self._products: Dict[str, Product] = {}
        self._products_fetched_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    @property
    def base(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}{self.cfg.api_prefix}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        """Issue one API call, with rate limiting and bounded retries."""
        url = f"{self.base}{path}"
        payload = json.dumps(body) if body is not None else None
        attempts = max(1, self.cfg.max_retries)
        delay = 0.5
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            self._limiter.acquire()
            headers = {"Content-Type": "application/json", "User-Agent": "coinbot/1.0"}
            if authenticated:
                if self.auth is None:
                    raise ExchangeError(
                        f"{path} requires credentials; none configured"
                    )
                # Signed per attempt: a JWT lives two minutes and a retry after
                # backoff could otherwise present an expired token.
                headers = self.auth.headers(method, url)

            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=payload,
                    headers=headers,
                    timeout=self.cfg.timeout_s,
                )
            except Exception as exc:  # network-level failure
                last_error = RetryableError(f"network error: {exc}", endpoint=path)
                if attempt == attempts:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
                continue

            if 200 <= response.status_code < 300:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise ExchangeError(
                        f"malformed JSON from {path}: {exc}", endpoint=path
                    ) from exc

            try:
                error_body = response.json()
            except ValueError:
                error_body = {"message": response.text[:500]}

            error = classify(response.status_code, error_body, path)
            if isinstance(error, RateLimitError):
                hint = response.headers.get("Retry-After")
                error.retry_after = float(hint) if hint else None

            if not isinstance(error, RetryableError) or attempt == attempts:
                raise error

            wait = getattr(error, "retry_after", None) or delay
            log.warning(
                "%s %s failed (%s), retry %d/%d in %.1fs",
                method, path, error, attempt, attempts, wait,
            )
            last_error = error
            time.sleep(wait)
            delay = min(delay * 2, 16.0)

        raise last_error or ExchangeError(f"{method} {path} failed", endpoint=path)

    # ------------------------------------------------------------------ #
    # Products
    # ------------------------------------------------------------------ #

    def get_products(self, *, force: bool = False, ttl: float = 3600.0) -> Dict[str, Product]:
        """All tradable products, cached (metadata changes rarely)."""
        if self._products and not force and now_ts() - self._products_fetched_at < ttl:
            return self._products
        data = self._request("GET", "/products", params={"product_type": "SPOT"})
        products: Dict[str, Product] = {}
        for raw in data.get("products", []):
            try:
                product = Product.from_coinbase(raw)
            except (KeyError, ValueError) as exc:
                log.debug("skipping unparseable product %s: %s", raw.get("product_id"), exc)
                continue
            products[product.product_id] = product
        if products:
            self._products = products
            self._products_fetched_at = now_ts()
        return self._products

    def get_product(self, product_id: str, *, force: bool = False) -> Product:
        if not force and product_id in self._products:
            return self._products[product_id]
        try:
            raw = self._request("GET", f"/products/{product_id}")
        except ExchangeError as exc:
            raise ProductNotFoundError(
                f"could not load product {product_id}: {exc}", endpoint=product_id
            ) from exc
        product = Product.from_coinbase(raw)
        self._products[product_id] = product
        return product

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def get_candles(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        start: Optional[int] = None,
        end: Optional[int] = None,
        limit: int = MAX_CANDLES_PER_REQUEST,
    ) -> List[Candle]:
        """Candles in ascending time order.

        Coinbase returns newest-first and caps the window, so this normalizes
        the order and derives ``start`` from ``limit`` when not supplied.
        """
        step = granularity.seconds
        end_ts = int(end if end is not None else now_ts())
        end_ts = floor_to_interval(end_ts, step) + step
        count = max(1, min(limit, MAX_CANDLES_PER_REQUEST))
        start_ts = int(start) if start is not None else end_ts - count * step

        data = self._request(
            "GET",
            f"/products/{product_id}/candles",
            params={
                "start": str(int(start_ts)),
                "end": str(int(end_ts)),
                "granularity": granularity.value,
                "limit": count,
            },
        )
        candles = [Candle.from_coinbase(raw) for raw in data.get("candles", [])]
        candles.sort(key=lambda c: c.start)
        return candles

    def get_candles_paged(
        self,
        product_id: str,
        granularity: Granularity,
        *,
        bars: int,
        end: Optional[int] = None,
    ) -> List[Candle]:
        """Walk backwards in pages to assemble more than one request's worth."""
        step = granularity.seconds
        remaining = max(1, bars)
        cursor = int(end if end is not None else now_ts())
        collected: Dict[int, Candle] = {}

        while remaining > 0:
            take = min(remaining, MAX_CANDLES_PER_REQUEST)
            page = self.get_candles(
                product_id, granularity,
                start=cursor - take * step, end=cursor, limit=take,
            )
            if not page:
                break
            for candle in page:
                collected[candle.start] = candle
            oldest = min(c.start for c in page)
            if oldest >= cursor:  # no progress; stop rather than spin
                break
            cursor = oldest
            remaining -= len(page)

        return [collected[k] for k in sorted(collected)]

    def get_quote(self, product_id: str) -> Optional[Quote]:
        """Top of book for one product."""
        data = self._request("GET", "/best_bid_ask", params={"product_ids": product_id})
        books = data.get("pricebooks", [])
        if not books:
            return None
        return self._parse_quote(books[0])

    def get_quotes(self, product_ids: Sequence[str]) -> Dict[str, Quote]:
        """Top of book for several products in one call."""
        if not product_ids:
            return {}
        # A list value encodes as a repeated query parameter, which is how
        # Coinbase expects multiple product ids.
        data = self._request(
            "GET", "/best_bid_ask", params={"product_ids": list(product_ids)}
        )
        out: Dict[str, Quote] = {}
        for book in data.get("pricebooks", []):
            quote = self._parse_quote(book)
            if quote is not None:
                out[quote.product_id] = quote
        return out

    @staticmethod
    def _parse_quote(book: Dict[str, Any]) -> Optional[Quote]:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            return Quote(
                product_id=book["product_id"],
                bid=to_decimal(bids[0]["price"]),
                ask=to_decimal(asks[0]["price"]),
                bid_size=to_decimal(bids[0].get("size", 0)),
                ask_size=to_decimal(asks[0].get("size", 0)),
            )
        except (KeyError, ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_accounts(self) -> Dict[str, Dict[str, Decimal]]:
        """Balances keyed by currency: ``{"USD": {"available":…, "hold":…}}``."""
        balances: Dict[str, Dict[str, Decimal]] = {}
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/accounts", params=params)
            for account in data.get("accounts", []):
                currency = account.get("currency")
                if not currency:
                    continue
                available = to_decimal((account.get("available_balance") or {}).get("value", 0))
                hold = to_decimal((account.get("hold") or {}).get("value", 0))
                existing = balances.setdefault(
                    currency, {"available": Decimal(0), "hold": Decimal(0)}
                )
                existing["available"] += available
                existing["hold"] += hold
            if not data.get("has_next"):
                break
            cursor = data.get("cursor")
            if not cursor:
                break
        return balances

    def get_fee_rates(self) -> Dict[str, float]:
        """Current maker/taker rates, as fractions (0.006 = 60 bps)."""
        data = self._request("GET", "/transaction_summary")
        tier = data.get("fee_tier", {}) or {}
        return {
            "maker": float(tier.get("maker_fee_rate", 0.004) or 0.004),
            "taker": float(tier.get("taker_fee_rate", 0.006) or 0.006),
        }

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(self, request: OrderRequest) -> Order:
        """Submit an order. The client_order_id makes submission idempotent."""
        request.validate()
        body = {
            "client_order_id": request.client_order_id,
            "product_id": request.product_id,
            "side": request.side.value,
            "order_configuration": self._order_configuration(request),
        }
        data = self._request("POST", "/orders", body=body)

        if not data.get("success", False):
            failure = data.get("error_response") or {}
            reason = (
                failure.get("message")
                or data.get("failure_reason")
                or "order rejected"
            )
            raise classify(400, {"error_response": failure, "message": reason}, "/orders")

        response = data.get("success_response") or {}
        order_id = response.get("order_id", "")
        return Order(
            order_id=order_id,
            client_order_id=request.client_order_id,
            product_id=request.product_id,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.PENDING,
            limit_price=request.limit_price,
            base_size=request.base_size,
            raw=data,
        )

    @staticmethod
    def _order_configuration(request: OrderRequest) -> Dict[str, Any]:
        """Translate an OrderRequest into Coinbase's order_configuration shape."""
        if request.order_type is OrderType.MARKET:
            config: Dict[str, Any] = {}
            if request.quote_size is not None:
                config["quote_size"] = str(request.quote_size)
            if request.base_size is not None:
                config["base_size"] = str(request.base_size)
            return {"market_market_ioc": config}

        if request.order_type is OrderType.LIMIT:
            config = {
                "base_size": str(request.base_size),
                "limit_price": str(request.limit_price),
            }
            if request.time_in_force is not None and request.time_in_force.value == "IOC":
                return {"sor_limit_ioc": config}
            config["post_only"] = bool(request.post_only)
            return {"limit_limit_gtc": config}

        if request.order_type is OrderType.STOP_LIMIT:
            return {
                "stop_limit_stop_limit_gtc": {
                    "base_size": str(request.base_size),
                    "limit_price": str(request.limit_price),
                    "stop_price": str(request.stop_price),
                    "stop_direction": request.stop_direction
                    or (
                        "STOP_DIRECTION_STOP_DOWN"
                        if request.side is Side.SELL
                        else "STOP_DIRECTION_STOP_UP"
                    ),
                }
            }

        raise ValueError(f"unsupported order type {request.order_type}")

    def get_order(self, order_id: str) -> Order:
        data = self._request("GET", f"/orders/historical/{order_id}")
        return self._parse_order(data.get("order", {}))

    def list_open_orders(self, product_id: Optional[str] = None) -> List[Order]:
        params: Dict[str, Any] = {"order_status": "OPEN", "limit": 100}
        if product_id:
            params["product_id"] = product_id
        data = self._request("GET", "/orders/historical/batch", params=params)
        return [self._parse_order(raw) for raw in data.get("orders", [])]

    def cancel_orders(self, order_ids: Iterable[str]) -> Dict[str, bool]:
        ids = [oid for oid in order_ids if oid]
        if not ids:
            return {}
        data = self._request("POST", "/orders/batch_cancel", body={"order_ids": ids})
        results: Dict[str, bool] = {}
        for entry in data.get("results", []):
            results[entry.get("order_id", "")] = bool(entry.get("success"))
        return results

    def cancel_order(self, order_id: str) -> bool:
        return self.cancel_orders([order_id]).get(order_id, False)

    def get_fills(
        self,
        *,
        order_id: Optional[str] = None,
        product_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Fill]:
        params: Dict[str, Any] = {"limit": limit}
        if order_id:
            params["order_ids"] = order_id
        if product_id:
            params["product_ids"] = product_id
        data = self._request("GET", "/orders/historical/fills", params=params)

        fills: List[Fill] = []
        for raw in data.get("fills", []):
            try:
                fills.append(
                    Fill(
                        order_id=raw.get("order_id", ""),
                        product_id=raw.get("product_id", ""),
                        side=Side(raw.get("side", "BUY").upper()),
                        size=to_decimal(raw.get("size", 0)),
                        price=to_decimal(raw.get("price", 0)),
                        fee=to_decimal(raw.get("commission", 0)),
                        ts=parse_iso(raw["trade_time"]) if raw.get("trade_time") else now_ts(),
                        trade_id=raw.get("trade_id", ""),
                    )
                )
            except (ValueError, KeyError) as exc:
                log.debug("skipping unparseable fill: %s", exc)
        return fills

    @staticmethod
    def _parse_order(raw: Dict[str, Any]) -> Order:
        config = raw.get("order_configuration", {}) or {}
        order_type = OrderType.MARKET
        limit_price: Optional[Decimal] = None
        base_size: Optional[Decimal] = None
        for key, cfg in config.items():
            if "limit" in key:
                order_type = OrderType.STOP_LIMIT if "stop" in key else OrderType.LIMIT
                if cfg.get("limit_price"):
                    limit_price = to_decimal(cfg["limit_price"])
            if cfg.get("base_size"):
                base_size = to_decimal(cfg["base_size"])

        filled = to_decimal(raw.get("filled_size", 0) or 0)
        avg_price = to_decimal(raw.get("average_filled_price", 0) or 0)
        return Order(
            order_id=raw.get("order_id", ""),
            client_order_id=raw.get("client_order_id", ""),
            product_id=raw.get("product_id", ""),
            side=Side(raw.get("side", "BUY").upper()),
            order_type=order_type,
            status=_STATUS_MAP.get(str(raw.get("status", "")).upper(), OrderStatus.UNKNOWN),
            limit_price=limit_price,
            base_size=base_size,
            filled_size=filled,
            avg_fill_price=avg_price,
            fee=to_decimal(raw.get("total_fees", 0) or 0),
            created_at=parse_iso(raw["created_time"]) if raw.get("created_time") else now_ts(),
            updated_at=now_ts(),
            reject_reason=raw.get("reject_reason", "") or "",
            raw=raw,
        )

    # ------------------------------------------------------------------ #

    def ping(self) -> bool:
        """Cheap connectivity and credential check."""
        try:
            self._request("GET", "/accounts", params={"limit": 1})
            return True
        except ExchangeError as exc:
            log.error("Coinbase ping failed: %s", exc)
            return False

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass


__all__ = ["MAX_CANDLES_PER_REQUEST", "CoinbaseClient"]
