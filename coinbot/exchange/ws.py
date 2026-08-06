"""Coinbase Advanced Trade WebSocket feed (optional).

The bot trades on bar closes and works fine on REST polling alone, so this is
strictly an enhancement: a live ticker lets stops react between bars instead of
waiting for the next close. It is therefore *optional in every direction* — the
``websockets`` dependency is imported lazily, and a feed that dies never takes
the trading loop with it. If the socket drops, the engine keeps running on REST
and the feed reconnects underneath it.

Runs its own asyncio loop on a background thread so callers stay synchronous.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence

from ..config import ApiConfig
from ..logging_setup import get_logger
from ..models import Quote
from ..util.numeric import to_decimal
from .auth import CoinbaseAuth

log = get_logger("ws")

TickerCallback = Callable[[Quote], None]


class CoinbaseWebSocket:
    """Background ticker feed with automatic reconnect.

    Subscribes to the ``ticker`` channel for the given products and keeps the
    latest quote per product available via :meth:`get_quote`.
    """

    def __init__(
        self,
        product_ids: Sequence[str],
        cfg: Optional[ApiConfig] = None,
        *,
        on_ticker: Optional[TickerCallback] = None,
        channel: str = "ticker",
    ) -> None:
        self.product_ids = list(product_ids)
        self.cfg = cfg or ApiConfig()
        self.on_ticker = on_ticker
        self.channel = channel

        self._quotes: Dict[str, Quote] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._last_message_at: float = 0.0
        self._auth: Optional[CoinbaseAuth] = None
        if self.cfg.has_credentials:
            try:
                self._auth = CoinbaseAuth(self.cfg.api_key, self.cfg.api_secret)
            except Exception as exc:  # pragma: no cover
                log.warning("websocket auth unavailable (%s); using public feed", exc)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, *, wait_s: float = 5.0) -> bool:
        """Start the feed. Returns True once connected (or False on timeout)."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.warning(
                "the 'websockets' package is not installed; "
                "continuing on REST polling only"
            )
            return False

        if self._thread and self._thread.is_alive():
            return self._connected.is_set()

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="coinbot-ws", daemon=True
        )
        self._thread.start()
        return self._connected.wait(timeout=wait_s)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._connected.clear()

    def __enter__(self) -> "CoinbaseWebSocket":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def get_quote(self, product_id: str) -> Optional[Quote]:
        with self._lock:
            return self._quotes.get(product_id)

    def get_quotes(self, product_ids: Optional[Sequence[str]] = None) -> Dict[str, Quote]:
        with self._lock:
            if product_ids is None:
                return dict(self._quotes)
            return {p: self._quotes[p] for p in product_ids if p in self._quotes}

    def is_stale(self, max_age_s: float = 30.0) -> bool:
        """True when no message has arrived recently — treat data as suspect."""
        if not self._last_message_at:
            return True
        return (time.time() - self._last_message_at) > max_age_s

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect_forever())
        except Exception:  # pragma: no cover - background thread guard
            log.exception("websocket thread exited unexpectedly")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._connected.clear()

    async def _connect_forever(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.cfg.ws_url, ping_interval=20, ping_timeout=20, max_size=2**22
                ) as socket:
                    await self._subscribe(socket)
                    self._connected.set()
                    backoff = 1.0
                    log.info("websocket connected: %s", ", ".join(self.product_ids))
                    await self._consume(socket)
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as exc:
                if self._stop.is_set():
                    break
                log.warning("websocket disconnected (%s); reconnecting in %.0fs", exc, backoff)
            finally:
                self._connected.clear()

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _subscribe(self, socket) -> None:
        message: Dict[str, object] = {
            "type": "subscribe",
            "product_ids": self.product_ids,
            "channel": self.channel,
        }
        if self._auth is not None:
            message["jwt"] = self._auth.websocket_token()
        await socket.send(json.dumps(message))

    async def _consume(self, socket) -> None:
        async for raw in socket:
            if self._stop.is_set():
                return
            self._last_message_at = time.time()
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._handle(payload)

    def _handle(self, payload: Dict[str, object]) -> None:
        channel = payload.get("channel")
        if channel == "subscriptions":
            return
        if channel not in ("ticker", "ticker_batch"):
            return

        for event in payload.get("events", []) or []:
            for tick in event.get("tickers", []) or []:
                quote = self._parse_ticker(tick)
                if quote is None:
                    continue
                with self._lock:
                    self._quotes[quote.product_id] = quote
                if self.on_ticker is not None:
                    try:
                        self.on_ticker(quote)
                    except Exception:
                        log.exception("ticker callback raised")

    @staticmethod
    def _parse_ticker(tick: Dict[str, object]) -> Optional[Quote]:
        product_id = tick.get("product_id")
        if not product_id:
            return None
        try:
            bid = to_decimal(tick.get("best_bid") or 0)
            ask = to_decimal(tick.get("best_ask") or 0)
            if bid <= 0 or ask <= 0:
                # Some ticker frames carry only a trade price.
                price = to_decimal(tick.get("price") or 0)
                if price <= 0:
                    return None
                half = price * Decimal("0.0001")
                bid, ask = price - half, price + half
            return Quote(
                product_id=str(product_id),
                bid=bid,
                ask=ask,
                bid_size=to_decimal(tick.get("best_bid_quantity") or 0),
                ask_size=to_decimal(tick.get("best_ask_quantity") or 0),
            )
        except (ValueError, TypeError, ArithmeticError):
            return None


__all__ = ["CoinbaseWebSocket"]
