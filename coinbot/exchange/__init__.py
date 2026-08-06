"""Coinbase Advanced Trade connectivity."""

from .auth import CoinbaseAuth
from .errors import (
    AuthenticationError,
    ExchangeError,
    InsufficientFundsError,
    InvalidOrderError,
    OrderNotFoundError,
    ProductNotFoundError,
    RateLimitError,
    RetryableError,
)
from .rest import CoinbaseClient

__all__ = [
    "AuthenticationError",
    "CoinbaseAuth",
    "CoinbaseClient",
    "ExchangeError",
    "InsufficientFundsError",
    "InvalidOrderError",
    "OrderNotFoundError",
    "ProductNotFoundError",
    "RateLimitError",
    "RetryableError",
]
