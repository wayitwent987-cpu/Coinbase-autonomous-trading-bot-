"""coinbot — a regime-aware autonomous trading bot for Coinbase Advanced Trade.

The design in one paragraph: a feature pipeline computes indicators once per
bar; a regime detector classifies the market into one of six states; a router
picks which strategies may trade that state and combines their opinions; a risk
manager decides whether and how large; an execution engine works the order.
Every layer is replaceable, and the same code runs in backtest, paper and live —
only the Broker changes.

    from coinbot import Config, TradingBot
    from coinbot.execution import PaperBroker

RISK: trading cryptocurrency is risky and this software can lose money.
Nothing here is financial advice. Run it on paper first.
"""

from .config import Config, ConfigError
from .models import (
    Candle,
    Granularity,
    Order,
    Position,
    Product,
    Regime,
    Side,
    Signal,
    TradeRecord,
)

__version__ = "1.0.0"

__all__ = [
    "Candle",
    "Config",
    "ConfigError",
    "Granularity",
    "Order",
    "Position",
    "Product",
    "Regime",
    "Side",
    "Signal",
    "TradeRecord",
    "TradingBot",
    "__version__",
]


def __getattr__(name: str):
    """Import the engine lazily.

    ``coinbot.engine`` pulls in the whole stack (exchange client, strategies,
    storage). Keeping it out of the eager import path means ``import coinbot``
    stays cheap and does not fail when an optional dependency is missing.
    """
    if name == "TradingBot":
        from .engine.bot import TradingBot

        return TradingBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
