"""Unit tests for :mod:`doghouse.sync`.

The HTTP side is exercised with a fake ``poster`` callable so the worker
can be driven one tick at a time without hitting a real server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from doghouse.buffer import Buffer, Reading
from doghouse.health import HealthState
from doghouse.sync import SyncResult, SyncResultKind, SyncWorker

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from doghouse.buffer import BufferedReading


def _sample(ts: float) -> Reading:
    return Reading(
        ts_unix=ts,
        installation_id="bus_hayfork_01",
        device_id="mppt_100_50_primary",
        raw={"V": "12800"},
        derived={"V": 12.8},
    )


class _ScriptedPoster:
    """Fake poster that returns results from a pre-baked script."""

    def __init__(self, script: list[SyncResult]) -> None:
        self._script = list(script)
        self.calls: list[int] = []

    async def __call__(self, readings: Sequence[BufferedReading]) -> SyncResult:
        self.calls.append(len(readings))
        if not self._script:
            raise RuntimeError("poster script exhausted")
        return self._script.pop(0)


async def _buf(tmp_path: Path, rows: int = 3) -> Buffer:
    buf = Buffer(tmp_path / "buffer.db")
    await buf.init()
    for i in range(rows):
        await buf.insert(_sample(float(i)))
    return buf


def _state() -> HealthState:
    return HealthState(started_at=0.0)


async def test_tick_on_empty_buffer_returns_interval(tmp_path: Path) -> None:
    buf = Buffer(tmp_path / "b.db")
    await buf.init()
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster([]),
        health_state=_state(),
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    assert await worker.tick() == 60.0
    await buf.close()


async def test_success_marks_synced_and_resets_backoff(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    state = HealthState(started_at=0.0)
    clock_values = iter([1234.5])
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster([SyncResult(kind=SyncResultKind.SUCCESS)]),
        health_state=state,
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
        clock=lambda: next(clock_values),
    )
    worker._backoff_s = 600.0
    sleep_s = await worker.tick()
    assert sleep_s == 60.0
    counts = await buf.counts()
    assert counts.unsynced == 0
    assert state.last_sync_ts == 1234.5
    assert state.last_sync_error is None
    assert worker.backoff_s == 30.0
    await buf.close()


async def test_transient_failure_escalates_backoff(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    state = HealthState(started_at=0.0)
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster(
            [
                SyncResult(kind=SyncResultKind.TRANSIENT, error="HTTP 503"),
                SyncResult(kind=SyncResultKind.TRANSIENT, error="HTTP 503"),
                SyncResult(kind=SyncResultKind.TRANSIENT, error="HTTP 503"),
            ]
        ),
        health_state=state,
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    sleeps = [await worker.tick() for _ in range(3)]
    assert sleeps == [30.0, 60.0, 120.0]
    # All rows still unsynced, each with incremented attempts.
    rows = await buf.fetch_unsynced(limit=10)
    assert all(r.sync_attempts == 3 for r in rows)
    assert state.last_sync_error == "HTTP 503"
    await buf.close()


async def test_transient_backoff_caps_at_max(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster(
            [SyncResult(kind=SyncResultKind.TRANSIENT, error="x") for _ in range(8)]
        ),
        health_state=_state(),
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=10.0,
        max_backoff_s=40.0,
    )
    sleeps = [await worker.tick() for _ in range(8)]
    # 10, 20, 40, 40, 40, 40, 40, 40
    assert sleeps[0] == 10.0
    assert sleeps[1] == 20.0
    assert all(s == 40.0 for s in sleeps[2:])
    await buf.close()


async def test_client_error_records_without_escalating(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    state = HealthState(started_at=0.0)
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster(
            [
                SyncResult(kind=SyncResultKind.CLIENT_ERROR, error="HTTP 400"),
                SyncResult(kind=SyncResultKind.CLIENT_ERROR, error="HTTP 400"),
            ]
        ),
        health_state=state,
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    for _ in range(2):
        assert await worker.tick() == 60.0
    assert worker.backoff_s == 30.0  # unchanged
    assert state.last_sync_error == "HTTP 400"
    rows = await buf.fetch_unsynced(limit=10)
    assert all(r.sync_attempts == 2 for r in rows)
    await buf.close()


async def test_success_after_failure_resets_backoff(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    state = HealthState(started_at=0.0)
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster(
            [
                SyncResult(kind=SyncResultKind.TRANSIENT, error="boom"),
                SyncResult(kind=SyncResultKind.TRANSIENT, error="boom"),
                SyncResult(kind=SyncResultKind.SUCCESS),
            ]
        ),
        health_state=state,
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    await worker.tick()
    await worker.tick()
    assert worker.backoff_s == 120.0  # escalated
    sleep_s = await worker.tick()
    assert sleep_s == 60.0
    assert worker.backoff_s == 30.0
    assert state.last_sync_error is None
    await buf.close()


async def test_batch_size_respected(tmp_path: Path) -> None:
    buf = Buffer(tmp_path / "b.db")
    await buf.init()
    for i in range(10):
        await buf.insert(_sample(float(i)))
    poster = _ScriptedPoster([SyncResult(kind=SyncResultKind.SUCCESS)])
    worker = SyncWorker(
        buffer=buf,
        poster=poster,
        health_state=_state(),
        interval_s=60.0,
        batch_size=3,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    await worker.tick()
    assert poster.calls == [3]
    counts = await buf.counts()
    assert counts.unsynced == 7
    await buf.close()


async def test_empty_poster_error_message_falls_back(tmp_path: Path) -> None:
    buf = await _buf(tmp_path)
    state = HealthState(started_at=0.0)
    worker = SyncWorker(
        buffer=buf,
        poster=_ScriptedPoster([SyncResult(kind=SyncResultKind.TRANSIENT)]),
        health_state=state,
        interval_s=60.0,
        batch_size=10,
        initial_backoff_s=30.0,
        max_backoff_s=1800.0,
    )
    await worker.tick()
    assert state.last_sync_error == "unknown error"
    await buf.close()


def test_rejects_bad_config() -> None:
    buf = MagicMock()
    poster = MagicMock()
    with pytest.raises(ValueError, match="interval_s"):
        SyncWorker(
            buffer=buf, poster=poster, health_state=_state(),
            interval_s=0, batch_size=1,
        )
    with pytest.raises(ValueError, match="backoff"):
        SyncWorker(
            buffer=buf, poster=poster, health_state=_state(),
            interval_s=60, batch_size=1,
            initial_backoff_s=100, max_backoff_s=50,
        )
