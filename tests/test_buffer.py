"""Unit tests for :mod:`doghouse.buffer`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest

from doghouse.buffer import Buffer, Reading

if TYPE_CHECKING:
    from pathlib import Path


def _sample(
    ts: float, inst: str = "bus_hayfork_01", device: str = "mppt_100_50_primary"
) -> Reading:
    return Reading(
        ts_unix=ts,
        installation_id=inst,
        device_id=device,
        raw={"V": "12800", "I": "5900"},
        derived={"V": 12.8, "I": 5.9, "P_battery_watts": 75.52, "error_name": None},
    )


async def _open(tmp_path: Path) -> Buffer:
    buf = Buffer(tmp_path / "buffer.db")
    await buf.init()
    return buf


async def test_init_creates_schema_and_enables_wal(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        async with aiosqlite.connect(tmp_path / "buffer.db") as conn, conn.execute(
            "PRAGMA journal_mode"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0].lower() == "wal"
    finally:
        await buf.close()


async def test_insert_returns_true_once_then_false_on_duplicate(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        assert await buf.insert(_sample(1.0)) is True
        assert await buf.insert(_sample(1.0)) is False
    finally:
        await buf.close()


async def test_different_installations_are_not_duplicates(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        assert await buf.insert(_sample(1.0, inst="a")) is True
        assert await buf.insert(_sample(1.0, inst="b")) is True
    finally:
        await buf.close()


async def test_fetch_unsynced_ordered_by_ts(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        for ts in (3.0, 1.0, 2.0):
            await buf.insert(_sample(ts))
        rows = await buf.fetch_unsynced(limit=10)
        assert [r.ts_unix for r in rows] == [1.0, 2.0, 3.0]
    finally:
        await buf.close()


async def test_fetch_unsynced_respects_limit(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        for ts in (1.0, 2.0, 3.0, 4.0):
            await buf.insert(_sample(ts))
        rows = await buf.fetch_unsynced(limit=2)
        assert [r.ts_unix for r in rows] == [1.0, 2.0]
    finally:
        await buf.close()


async def test_fetch_unsynced_limit_must_be_positive(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="limit"):
            await buf.fetch_unsynced(limit=0)
    finally:
        await buf.close()


async def test_mark_synced_removes_from_unsynced_queue(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        for ts in (1.0, 2.0, 3.0):
            await buf.insert(_sample(ts))
        rows = await buf.fetch_unsynced(limit=10)
        updated = await buf.mark_synced([r.id for r in rows[:2]])
        assert updated == 2
        remaining = await buf.fetch_unsynced(limit=10)
        assert [r.ts_unix for r in remaining] == [3.0]
    finally:
        await buf.close()


async def test_mark_synced_empty_is_noop(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        assert await buf.mark_synced([]) == 0
    finally:
        await buf.close()


async def test_mark_failed_increments_attempts_and_records_error(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        await buf.insert(_sample(1.0))
        [row] = await buf.fetch_unsynced(limit=1)
        assert row.sync_attempts == 0
        await buf.mark_failed([row.id], "connection refused")
        await buf.mark_failed([row.id], "timeout")
        [row2] = await buf.fetch_unsynced(limit=1)
        assert row2.sync_attempts == 2
    finally:
        await buf.close()


async def test_mark_failed_keeps_row_unsynced(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        await buf.insert(_sample(1.0))
        [row] = await buf.fetch_unsynced(limit=1)
        await buf.mark_failed([row.id], "boom")
        assert len(await buf.fetch_unsynced(limit=1)) == 1
    finally:
        await buf.close()


async def test_mark_synced_clears_last_sync_error(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        await buf.insert(_sample(1.0))
        [row] = await buf.fetch_unsynced(limit=1)
        await buf.mark_failed([row.id], "boom")
        await buf.mark_synced([row.id])
        # Peek directly at the stored row to confirm last_sync_error cleared.
        async with aiosqlite.connect(buf._db_path) as conn, conn.execute(
            "SELECT synced, last_sync_error FROM readings WHERE id = ?", (row.id,)
        ) as cur:
            stored = await cur.fetchone()
        assert stored is not None
        assert stored[0] == 1
        assert stored[1] is None
    finally:
        await buf.close()


async def test_counts(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        for ts in (1.0, 2.0, 3.0):
            await buf.insert(_sample(ts))
        rows = await buf.fetch_unsynced(limit=10)
        await buf.mark_synced([rows[0].id])
        counts = await buf.counts()
        assert counts.total == 3
        assert counts.unsynced == 2
    finally:
        await buf.close()


async def test_counts_empty(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        counts = await buf.counts()
        assert counts.total == 0
        assert counts.unsynced == 0
    finally:
        await buf.close()


async def test_last_reading_ts_empty(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        assert await buf.last_reading_ts() is None
    finally:
        await buf.close()


async def test_last_reading_ts_returns_max(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        for ts in (5.0, 1.0, 3.0):
            await buf.insert(_sample(ts))
        assert await buf.last_reading_ts() == 5.0
    finally:
        await buf.close()


async def test_raw_and_derived_roundtrip(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    try:
        await buf.insert(_sample(1.0))
        [row] = await buf.fetch_unsynced(limit=1)
        assert row.raw == {"V": "12800", "I": "5900"}
        assert row.derived == {
            "V": 12.8,
            "I": 5.9,
            "P_battery_watts": 75.52,
            "error_name": None,
        }
    finally:
        await buf.close()


async def test_use_before_init_raises(tmp_path: Path) -> None:
    buf = Buffer(tmp_path / "buffer.db")
    with pytest.raises(RuntimeError, match="init"):
        await buf.insert(_sample(1.0))


async def test_close_is_idempotent(tmp_path: Path) -> None:
    buf = await _open(tmp_path)
    await buf.close()
    await buf.close()


async def test_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "buffer.db"
    buf = Buffer(nested)
    try:
        await buf.init()
        assert nested.exists()
    finally:
        await buf.close()
