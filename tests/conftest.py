"""Shared pytest fixtures and import path setup."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Make both the package and the synthetic-tape helper importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from coinbot.config import Config  # noqa: E402
from coinbot.models import Product  # noqa: E402


@pytest.fixture
def product() -> Product:
    """A BTC-USD-like product with realistic increments."""
    return Product(
        product_id="BTC-USD",
        base_currency="BTC",
        quote_currency="USD",
        base_increment=Decimal("0.00000001"),
        quote_increment=Decimal("0.01"),
        price_increment=Decimal("0.01"),
        min_base_size=Decimal("0.000016"),
        min_quote_size=Decimal("1"),
    )


@pytest.fixture
def config() -> Config:
    """A default config narrowed to one product, safe for tests."""
    cfg = Config()
    cfg.trading.products = ["BTC-USD"]
    cfg.trading.warmup_bars = 250
    cfg.trading.dry_run = False  # tests drive a paper broker
    cfg.backtest.initial_equity = 10_000.0
    cfg.storage.db_path = ":memory:"
    return cfg
