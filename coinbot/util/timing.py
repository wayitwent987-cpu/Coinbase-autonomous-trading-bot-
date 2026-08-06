"""Time and retry helpers."""

from __future__ import annotations

import functools
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def now_ts() -> float:
    return time.time()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def iso(ts: float) -> str:
    return to_utc(ts).isoformat(timespec="seconds")


def parse_iso(text: str) -> float:
    """Parse an RFC3339/ISO timestamp (Coinbase uses trailing 'Z')."""
    cleaned = text.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).timestamp()


def floor_to_interval(ts: float, interval_s: int) -> int:
    """Bucket a timestamp down to the start of its candle."""
    return int(ts // interval_s) * interval_s


def next_boundary(ts: float, interval_s: int) -> int:
    return floor_to_interval(ts, interval_s) + interval_s


def sleep_until_next_bar(interval_s: int, offset_s: float = 2.0) -> float:
    """Block until just after the next bar closes; returns seconds slept.

    The offset gives the exchange a moment to finalize the closing candle so
    we do not act on a bar that is still being written.
    """
    target = next_boundary(time.time(), interval_s) + offset_s
    delay = max(0.0, target - time.time())
    if delay:
        time.sleep(delay)
    return delay


class RateLimiter:
    """Token-bucket limiter, sized for Coinbase's per-endpoint request caps."""

    def __init__(self, rate_per_sec: float, burst: Optional[int] = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = float(rate_per_sec)
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_sec))
        self._tokens = self.capacity
        self._last = time.monotonic()

    def acquire(self, tokens: float = 1.0) -> float:
        """Consume tokens, sleeping if the bucket is dry. Returns time waited."""
        waited = 0.0
        while True:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self.rate
            )
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return waited
            deficit = tokens - self._tokens
            delay = deficit / self.rate
            time.sleep(delay)
            waited += delay


def retry(
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 16.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    jitter: bool = True,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Exponential-backoff retry decorator for transient network failures."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            delay = base_delay
            last: Optional[BaseException] = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203 - retry is the point
                    last = exc
                    if attempt == attempts:
                        break
                    if on_retry:
                        on_retry(attempt, exc)
                    else:
                        log.warning(
                            "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                            fn.__name__, attempt, attempts, exc, delay,
                        )
                    sleep_for = delay * (0.5 + random.random()) if jitter else delay
                    time.sleep(min(sleep_for, max_delay))
                    delay = min(delay * 2, max_delay)
            assert last is not None
            raise last

        return wrapper

    return decorator


def chunked(items: Iterable[T], size: int) -> Iterable[list]:
    """Yield lists of at most ``size`` items."""
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def humanize_duration(seconds: float) -> str:
    delta = timedelta(seconds=int(max(0, seconds)))
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


__all__ = [
    "RateLimiter",
    "chunked",
    "floor_to_interval",
    "humanize_duration",
    "iso",
    "next_boundary",
    "now_ts",
    "now_utc",
    "parse_iso",
    "retry",
    "sleep_until_next_bar",
    "to_utc",
]
