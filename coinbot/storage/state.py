"""SQLite persistence.

A trading bot restarts — deploys, crashes, reboots. If it forgets its open
positions, stop levels, and drawdown peak, it will happily re-enter a market it
was already in and reset the very circuit breaker that was protecting it. This
store keeps enough state that a restart resumes rather than begins.

Written synchronously with WAL enabled: durability matters more than write
throughput at a few rows per bar.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from ..models import Position, Regime, Side, TradeRecord
from ..util.numeric import to_decimal
from ..util.timing import now_ts

log = get_logger("storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    product_id      TEXT PRIMARY KEY,
    size            TEXT NOT NULL,
    entry_price     TEXT NOT NULL,
    stop_price      TEXT,
    take_profit     TEXT,
    trail_anchor    TEXT,
    strategy        TEXT,
    regime          TEXT,
    opened_at       REAL,
    bars_held       INTEGER DEFAULT 0,
    realized_pnl    TEXT DEFAULT '0',
    fees_paid       TEXT DEFAULT '0',
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL,
    strategy     TEXT,
    regime       TEXT,
    side         TEXT NOT NULL,
    size         TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    exit_price   TEXT NOT NULL,
    pnl          TEXT NOT NULL,
    fees         TEXT NOT NULL,
    opened_at    REAL,
    closed_at    REAL,
    exit_reason  TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts     REAL PRIMARY KEY,
    equity TEXT NOT NULL,
    cash   TEXT,
    exposure TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    level TEXT NOT NULL,
    kind  TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_product ON trades(product_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class StateStore:
    """Durable bot state. Safe to construct against a fresh or existing file."""

    def __init__(self, path: str = "state/coinbot.sqlite3") -> None:
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Meta
    # ------------------------------------------------------------------ #

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    # ------------------------------------------------------------------ #
    # Positions
    # ------------------------------------------------------------------ #

    def save_position(self, position: Position) -> None:
        if not position.is_open:
            self.delete_position(position.product_id)
            return
        self.conn.execute(
            """
            INSERT INTO positions(
                product_id, size, entry_price, stop_price, take_profit,
                trail_anchor, strategy, regime, opened_at, bars_held,
                realized_pnl, fees_paid, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(product_id) DO UPDATE SET
                size=excluded.size, entry_price=excluded.entry_price,
                stop_price=excluded.stop_price, take_profit=excluded.take_profit,
                trail_anchor=excluded.trail_anchor, strategy=excluded.strategy,
                regime=excluded.regime, opened_at=excluded.opened_at,
                bars_held=excluded.bars_held, realized_pnl=excluded.realized_pnl,
                fees_paid=excluded.fees_paid, updated_at=excluded.updated_at
            """,
            (
                position.product_id,
                str(position.size),
                str(position.entry_price),
                str(position.stop_price) if position.stop_price is not None else None,
                str(position.take_profit) if position.take_profit is not None else None,
                str(position.trail_anchor) if position.trail_anchor is not None else None,
                position.strategy,
                position.regime_at_entry.value,
                position.opened_at,
                position.bars_held,
                str(position.realized_pnl),
                str(position.fees_paid),
                now_ts(),
            ),
        )
        self.conn.commit()

    def delete_position(self, product_id: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE product_id = ?", (product_id,))
        self.conn.commit()

    def load_positions(self) -> Dict[str, Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        out: Dict[str, Position] = {}
        for row in rows:
            try:
                position = Position(
                    product_id=row["product_id"],
                    size=to_decimal(row["size"]),
                    entry_price=to_decimal(row["entry_price"]),
                    stop_price=to_decimal(row["stop_price"]) if row["stop_price"] else None,
                    take_profit=to_decimal(row["take_profit"]) if row["take_profit"] else None,
                    trail_anchor=to_decimal(row["trail_anchor"]) if row["trail_anchor"] else None,
                    strategy=row["strategy"] or "",
                    regime_at_entry=_safe_regime(row["regime"]),
                    opened_at=row["opened_at"],
                    bars_held=row["bars_held"] or 0,
                    realized_pnl=to_decimal(row["realized_pnl"] or 0),
                    fees_paid=to_decimal(row["fees_paid"] or 0),
                )
            except (ArithmeticError, ValueError, TypeError) as exc:
                log.error("skipping corrupt stored position %s: %s", row["product_id"], exc)
                continue
            out[position.product_id] = position
        return out

    # ------------------------------------------------------------------ #
    # Trades
    # ------------------------------------------------------------------ #

    def record_trade(self, trade: TradeRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO trades(
                product_id, strategy, regime, side, size, entry_price,
                exit_price, pnl, fees, opened_at, closed_at, exit_reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade.product_id, trade.strategy, trade.regime.value, trade.side.value,
                str(trade.size), str(trade.entry_price), str(trade.exit_price),
                str(trade.pnl), str(trade.fees), trade.opened_at, trade.closed_at,
                trade.exit_reason,
            ),
        )
        self.conn.commit()

    def load_trades(self, limit: int = 500) -> List[TradeRecord]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        trades: List[TradeRecord] = []
        for row in reversed(rows):
            try:
                trades.append(
                    TradeRecord(
                        product_id=row["product_id"],
                        strategy=row["strategy"] or "",
                        side=Side(row["side"]),
                        size=to_decimal(row["size"]),
                        entry_price=to_decimal(row["entry_price"]),
                        exit_price=to_decimal(row["exit_price"]),
                        pnl=to_decimal(row["pnl"]),
                        fees=to_decimal(row["fees"]),
                        opened_at=row["opened_at"] or 0.0,
                        closed_at=row["closed_at"] or 0.0,
                        regime=_safe_regime(row["regime"]),
                        exit_reason=row["exit_reason"] or "",
                    )
                )
            except (ArithmeticError, ValueError, TypeError) as exc:
                log.error("skipping corrupt stored trade: %s", exc)
        return trades

    def trade_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Equity curve and events
    # ------------------------------------------------------------------ #

    def record_equity(
        self,
        equity: Decimal,
        cash: Optional[Decimal] = None,
        exposure: Optional[Decimal] = None,
        ts: Optional[float] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO equity(ts, equity, cash, exposure) VALUES(?,?,?,?) "
            "ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, "
            "cash=excluded.cash, exposure=excluded.exposure",
            (
                ts if ts is not None else now_ts(),
                str(equity),
                str(cash) if cash is not None else None,
                str(exposure) if exposure is not None else None,
            ),
        )
        self.conn.commit()

    def load_equity_curve(self, limit: int = 10_000) -> List[tuple]:
        rows = self.conn.execute(
            "SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(row["ts"], to_decimal(row["equity"])) for row in reversed(rows)]

    def record_event(self, kind: str, detail: Any = None, level: str = "INFO") -> None:
        self.conn.execute(
            "INSERT INTO events(ts, level, kind, detail) VALUES(?,?,?,?)",
            (now_ts(), level, kind, json.dumps(detail, default=str) if detail else None),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #

    def vacuum(self, keep_equity_rows: int = 50_000) -> None:
        """Trim the equity table so a long-running bot's file stays bounded."""
        self.conn.execute(
            "DELETE FROM equity WHERE ts NOT IN "
            "(SELECT ts FROM equity ORDER BY ts DESC LIMIT ?)",
            (keep_equity_rows,),
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _safe_regime(value: Optional[str]) -> Regime:
    try:
        return Regime(value) if value else Regime.UNKNOWN
    except ValueError:
        return Regime.UNKNOWN


__all__ = ["StateStore"]
