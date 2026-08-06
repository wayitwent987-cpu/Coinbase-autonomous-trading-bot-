"""Exchange error types.

The distinction that matters is :class:`RetryableError` versus everything
else. Retrying a rate-limit or a 502 is correct; retrying "insufficient funds"
or "invalid product" just burns rate limit and delays the real fix.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ExchangeError(Exception):
    """Base class for every exchange failure."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
        endpoint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload or {}
        self.endpoint = endpoint

    def __str__(self) -> str:
        parts = [self.message]
        if self.status:
            parts.append(f"status={self.status}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        return " ".join(parts)


class RetryableError(ExchangeError):
    """Transient: retrying the identical request is safe and sensible."""


class RateLimitError(RetryableError):
    """HTTP 429. ``retry_after`` is the server's hint, in seconds."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class AuthenticationError(ExchangeError):
    """Bad, expired, or insufficiently scoped credentials. Never retried."""


class InsufficientFundsError(ExchangeError):
    """The account cannot cover the order."""


class InvalidOrderError(ExchangeError):
    """The order violates product rules (size, increment, price bounds)."""


class OrderNotFoundError(ExchangeError):
    """The referenced order does not exist."""


class ProductNotFoundError(ExchangeError):
    """Unknown or delisted product id."""


# Coinbase returns machine-readable strings in ``error`` / ``error_response``;
# map the ones worth reacting to differently.
_ERROR_MAP = {
    "INSUFFICIENT_FUND": InsufficientFundsError,
    "INSUFFICIENT_FUNDS": InsufficientFundsError,
    "INVALID_SIDE": InvalidOrderError,
    "INVALID_PRODUCT_ID": ProductNotFoundError,
    "INVALID_SIZE_PRECISION": InvalidOrderError,
    "INVALID_PRICE_PRECISION": InvalidOrderError,
    "INVALID_LIMIT_PRICE": InvalidOrderError,
    "SIZE_TOO_SMALL": InvalidOrderError,
    "SIZE_TOO_LARGE": InvalidOrderError,
    "UNKNOWN_ORDER": OrderNotFoundError,
    "UNAUTHORIZED": AuthenticationError,
    "PERMISSION_DENIED": AuthenticationError,
}


def classify(
    status: int,
    payload: Optional[Dict[str, Any]],
    endpoint: str = "",
) -> ExchangeError:
    """Turn an HTTP status plus response body into the right exception."""
    payload = payload or {}
    code = str(
        payload.get("error")
        or payload.get("error_response", {}).get("error")
        or ""
    ).upper()
    message = (
        payload.get("message")
        or payload.get("error_response", {}).get("message")
        or payload.get("error_details")
        or f"HTTP {status}"
    )

    specific = _ERROR_MAP.get(code)
    if specific is not None:
        return specific(message, status=status, payload=payload, endpoint=endpoint)

    if status == 429:
        return RateLimitError(message, status=status, payload=payload, endpoint=endpoint)
    if status in (401, 403):
        return AuthenticationError(message, status=status, payload=payload, endpoint=endpoint)
    if status == 404:
        return OrderNotFoundError(message, status=status, payload=payload, endpoint=endpoint)
    if status >= 500 or status == 408:
        return RetryableError(message, status=status, payload=payload, endpoint=endpoint)
    return ExchangeError(message, status=status, payload=payload, endpoint=endpoint)


__all__ = [
    "AuthenticationError",
    "ExchangeError",
    "InsufficientFundsError",
    "InvalidOrderError",
    "OrderNotFoundError",
    "ProductNotFoundError",
    "RateLimitError",
    "RetryableError",
    "classify",
]
