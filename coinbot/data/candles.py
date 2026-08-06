"""A bounded, gap-aware, de-duplicated candle buffer.

The exchange happily returns overlapping windows and revises the most recent
(still-forming) bar, so the buffer is keyed by bar start time and upserts.
Only *closed* bars are handed to strategies — acting on a forming bar is the
classic way to backtest beautifully and lose money live.
"""

from __future__ import annotations

import bisect
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from ..models import Candle
from ..util.timing import floor_to_interval, now_ts


class CandleSeries:
    """Ordered candle history for one product at one granularity."""

    __slots__ = ("product_id", "interval_s", "max_size", "_candles", "_index")

    def __init__(self, product_id: str, interval_s: int, max_size: int = 1000) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if max_size < 2:
            raise ValueError("max_size must be at least 2")
        self.product_id = product_id
        self.interval_s = interval_s
        self.max_size = max_size
        self._candles: List[Candle] = []
        self._index: Dict[int, int] = {}  # bar start -> position

    # ------------------------------------------------------------------ #
    # Sequence protocol
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._candles)

    def __iter__(self) -> Iterator[Candle]:
        return iter(self._candles)

    def __getitem__(self, item):  # type: ignore[no-untyped-def]
        return self._candles[item]

    def __bool__(self) -> bool:
        return bool(self._candles)

    def __repr__(self) -> str:  # pragma: no cover - display only
        span = f"{self.first.start}..{self.last.start}" if self._candles else "empty"
        return f"<CandleSeries {self.product_id} {self.interval_s}s n={len(self)} {span}>"

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def candles(self) -> List[Candle]:
        return self._candles

    @property
    def first(self) -> Candle:
        return self._candles[0]

    @property
    def last(self) -> Candle:
        return self._candles[-1]

    @property
    def closes(self) -> List[float]:
        return [c.close for c in self._candles]

    @property
    def highs(self) -> List[float]:
        return [c.high for c in self._candles]

    @property
    def lows(self) -> List[float]:
        return [c.low for c in self._candles]

    @property
    def volumes(self) -> List[float]:
        return [c.volume for c in self._candles]

    @property
    def last_close(self) -> Optional[float]:
        return self._candles[-1].close if self._candles else None

    def get(self, start: int) -> Optional[Candle]:
        pos = self._index.get(start)
        return self._candles[pos] if pos is not None else None

    def tail(self, n: int) -> List[Candle]:
        return self._candles[-n:] if n > 0 else []

    def ready(self, minimum: int) -> bool:
        return len(self._candles) >= minimum

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def add(self, candle: Candle) -> bool:
        """Insert or replace one candle. Returns True if this is a *new* bar."""
        aligned = floor_to_interval(candle.start, self.interval_s)
        if aligned != candle.start:
            candle = Candle(
                start=aligned,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )

        existing = self._index.get(candle.start)
        if existing is not None:
            # Same bar, revised values (the exchange updates the live bar).
            self._candles[existing] = candle
            return False

        if not self._candles or candle.start > self._candles[-1].start:
            self._candles.append(candle)
            self._index[candle.start] = len(self._candles) - 1
            self._trim()
            return True

        # Out-of-order arrival: insert in place and rebuild the index.
        starts = [c.start for c in self._candles]
        pos = bisect.bisect_left(starts, candle.start)
        self._candles.insert(pos, candle)
        self._reindex()
        self._trim()
        return True

    def extend(self, candles: Iterable[Candle]) -> int:
        """Merge a batch; returns how many were new bars."""
        added = 0
        for candle in candles:
            if self.add(candle):
                added += 1
        return added

    def replace_all(self, candles: Sequence[Candle]) -> None:
        ordered = sorted(candles, key=lambda c: c.start)
        self._candles = list(ordered)[-self.max_size:]
        self._reindex()

    def clear(self) -> None:
        self._candles.clear()
        self._index.clear()

    def _trim(self) -> None:
        overflow = len(self._candles) - self.max_size
        if overflow > 0:
            del self._candles[:overflow]
            self._reindex()

    def _reindex(self) -> None:
        self._index = {c.start: i for i, c in enumerate(self._candles)}

    # ------------------------------------------------------------------ #
    # Freshness / integrity
    # ------------------------------------------------------------------ #

    def closed_candles(self, *, reference_ts: Optional[float] = None) -> List[Candle]:
        """History excluding any bar that has not finished forming."""
        if not self._candles:
            return []
        ref = reference_ts if reference_ts is not None else now_ts()
        current_bar = floor_to_interval(ref, self.interval_s)
        return [c for c in self._candles if c.start < current_bar]

    def last_closed(self, *, reference_ts: Optional[float] = None) -> Optional[Candle]:
        closed = self.closed_candles(reference_ts=reference_ts)
        return closed[-1] if closed else None

    def gaps(self) -> List[tuple]:
        """Missing-bar ranges as ``(after_start, before_start, missing_count)``.

        Crypto trades continuously, so a gap means dropped data rather than a
        market close — worth logging before trusting an indicator over it.
        """
        out: List[tuple] = []
        for prev, cur in zip(self._candles, self._candles[1:]):
            step = cur.start - prev.start
            if step > self.interval_s:
                missing = step // self.interval_s - 1
                out.append((prev.start, cur.start, int(missing)))
        return out

    def is_stale(self, max_age_bars: float = 2.0, *, reference_ts: Optional[float] = None) -> bool:
        """True when the newest bar is older than ``max_age_bars`` intervals."""
        if not self._candles:
            return True
        ref = reference_ts if reference_ts is not None else now_ts()
        age = ref - (self._candles[-1].start + self.interval_s)
        return age > max_age_bars * self.interval_s

    def missing_range(self, *, reference_ts: Optional[float] = None) -> Optional[tuple]:
        """``(start_ts, end_ts)`` needed to catch up, or ``None`` if current."""
        ref = reference_ts if reference_ts is not None else now_ts()
        current_bar = floor_to_interval(ref, self.interval_s)
        if not self._candles:
            return None
        newest = self._candles[-1].start
        if newest >= current_bar:
            return None
        return (newest, current_bar + self.interval_s)


__all__ = ["CandleSeries"]
