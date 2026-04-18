"""SQLite-backed telemetry buffer with sync bookkeeping.

Readings are persisted locally as JSON blobs keyed by
``(installation_id, ts_unix)`` so duplicate samples cannot clobber earlier
rows. The buffer never auto-deletes; the sync worker is responsible for
marking rows synced once SOPHIA accepts them. Rows are preserved even
after a successful push so disputes or replays stay possible.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_LOG = logging.getLogger(__name__)

_BUFFER_WARN_BYTES: Final[int] = 1_073_741_824  # 1 GiB

_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix REAL NOT NULL,
    installation_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    derived_json TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0,
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    last_sync_error TEXT,
    UNIQUE(installation_id, ts_unix)
);
CREATE INDEX IF NOT EXISTS idx_unsynced ON readings(synced, ts_unix);
CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts_unix);
"""


@dataclass(frozen=True, slots=True)
class Reading:
    """A VE.Direct sample ready for insertion into the buffer."""

    ts_unix: float
    installation_id: str
    device_id: str
    raw: dict[str, str]
    derived: dict[str, float | str | None]


@dataclass(frozen=True, slots=True)
class BufferedReading:
    """A row read back from the buffer, with its assigned id."""

    id: int
    ts_unix: float
    installation_id: str
    device_id: str
    raw: dict[str, str]
    derived: dict[str, float | str | None]
    sync_attempts: int


@dataclass(frozen=True, slots=True)
class BufferCounts:
    """Row totals used by the health endpoint."""

    total: int
    unsynced: int


class Buffer:
    """Async SQLite buffer for telemetry readings.

    Lifecycle:

    1. ``buf = Buffer(path)`` — cheap, no I/O.
    2. ``await buf.init()`` — opens the DB, ensures schema + WAL.
    3. ``await buf.insert(reading)`` / ``await buf.fetch_unsynced(n)`` /
       ``await buf.mark_synced(ids)`` / ``await buf.mark_failed(ids, err)``.
    4. ``await buf.close()`` on shutdown.

    One :class:`Buffer` owns one :class:`aiosqlite.Connection`. The
    connection serializes SQL on its own worker thread, so no external
    locking is required for the single-writer pattern used here.
    """

    def __init__(self, db_path: Path) -> None:
        """Store the DB path. No files are opened or created here."""
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the DB, create schema if needed, enable WAL.

        Warns at WARNING level if the DB file is already larger than 1 GiB.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.commit()
        self._warn_if_oversize()

    async def insert(self, reading: Reading) -> bool:
        """Insert a reading; return ``True`` if stored, ``False`` if duplicate.

        Duplicates are identified by the ``UNIQUE(installation_id, ts_unix)``
        constraint; they are silently ignored via ``INSERT OR IGNORE``.
        """
        conn = self._require()
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO readings "
            "(ts_unix, installation_id, device_id, raw_json, derived_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                reading.ts_unix,
                reading.installation_id,
                reading.device_id,
                json.dumps(reading.raw, sort_keys=True),
                json.dumps(reading.derived, sort_keys=True),
            ),
        )
        await conn.commit()
        return cursor.rowcount == 1

    async def fetch_unsynced(self, limit: int) -> list[BufferedReading]:
        """Return up to ``limit`` oldest unsynced rows, ordered by timestamp."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        conn = self._require()
        async with conn.execute(
            "SELECT id, ts_unix, installation_id, device_id, raw_json, "
            "derived_json, sync_attempts "
            "FROM readings WHERE synced = 0 ORDER BY ts_unix ASC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_buffered(row) for row in rows]

    async def mark_synced(self, ids: Sequence[int]) -> int:
        """Mark the given rows synced; returns the number of rows updated."""
        if not ids:
            return 0
        conn = self._require()
        placeholders = ",".join("?" for _ in ids)
        cursor = await conn.execute(
            "UPDATE readings SET synced = 1, last_sync_error = NULL "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        )
        await conn.commit()
        return cursor.rowcount or 0

    async def mark_failed(self, ids: Sequence[int], error: str) -> int:
        """Record a failed sync attempt for the given rows.

        Increments ``sync_attempts`` and stores ``error`` on each row.
        Does not flip ``synced``; callers keep retrying these rows until
        SOPHIA accepts them.
        """
        if not ids:
            return 0
        conn = self._require()
        placeholders = ",".join("?" for _ in ids)
        cursor = await conn.execute(
            "UPDATE readings "  # noqa: S608
            "SET sync_attempts = sync_attempts + 1, last_sync_error = ? "
            f"WHERE id IN ({placeholders})",
            (error, *ids),
        )
        await conn.commit()
        return cursor.rowcount or 0

    async def counts(self) -> BufferCounts:
        """Return total and unsynced row counts."""
        conn = self._require()
        async with conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN synced = 0 THEN 1 ELSE 0 END), 0) "
            "FROM readings"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return BufferCounts(total=0, unsynced=0)
        return BufferCounts(total=int(row[0]), unsynced=int(row[1]))

    async def last_reading_ts(self) -> float | None:
        """Return the max ``ts_unix`` across all rows, or ``None`` if empty."""
        conn = self._require()
        async with conn.execute("SELECT MAX(ts_unix) FROM readings") as cur:
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    def _require(self) -> aiosqlite.Connection:
        """Return the open connection; raise if :meth:`init` was skipped."""
        if self._conn is None:
            raise RuntimeError("Buffer not initialized; call init() first")
        return self._conn

    def _warn_if_oversize(self) -> None:
        """Log at WARNING level if the DB file exceeds 1 GiB."""
        try:
            size = self._db_path.stat().st_size
        except FileNotFoundError:
            return
        if size > _BUFFER_WARN_BYTES:
            _LOG.warning(
                "buffer DB size %d bytes exceeds %d-byte threshold",
                size,
                _BUFFER_WARN_BYTES,
            )


def _row_to_buffered(row: Any) -> BufferedReading:
    """Convert a sqlite row tuple into a typed :class:`BufferedReading`."""
    return BufferedReading(
        id=int(row[0]),
        ts_unix=float(row[1]),
        installation_id=str(row[2]),
        device_id=str(row[3]),
        raw=json.loads(row[4]),
        derived=json.loads(row[5]),
        sync_attempts=int(row[6]),
    )
