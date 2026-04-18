"""Unit tests for :mod:`doghouse.health`."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from doghouse.buffer import Buffer, BufferCounts, Reading
from doghouse.health import HealthServer, HealthState

if TYPE_CHECKING:
    from pathlib import Path


def _fake_reader(*, connected: bool) -> MagicMock:
    mock = MagicMock()
    type(mock).connected = connected
    return mock


def _make_buffer_mock(total: int, unsynced: int, last_reading: float | None) -> MagicMock:
    mock = MagicMock()
    mock.counts = AsyncMock(return_value=BufferCounts(total=total, unsynced=unsynced))
    mock.last_reading_ts = AsyncMock(return_value=last_reading)
    return mock


async def test_snapshot_reports_all_fields() -> None:
    state = HealthState(
        started_at=time.time() - 10,
        last_sync_ts=1234.0,
        last_sync_error=None,
    )
    buffer = _make_buffer_mock(total=48290, unsynced=12, last_reading=4567.8)
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        state=state,
        buffer=buffer,
        reader=_fake_reader(connected=True),
        hostname_tag="doghouse",
        source_type="solar_mppt",
    )
    snap = await server.snapshot()
    assert snap["hostname"] == "doghouse"
    assert snap["source_type"] == "solar_mppt"
    assert snap["buffer_total"] == 48290
    assert snap["buffer_unsynced"] == 12
    assert snap["last_reading_ts"] == 4567.8
    assert snap["last_sync_ts"] == 1234.0
    assert snap["last_sync_error"] is None
    assert snap["serial_connected"] is True
    assert snap["uptime_s"] >= 9


async def test_snapshot_reports_disconnected_reader() -> None:
    state = HealthState(started_at=time.time())
    buffer = _make_buffer_mock(total=0, unsynced=0, last_reading=None)
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        state=state,
        buffer=buffer,
        reader=_fake_reader(connected=False),
        hostname_tag="doghouse",
        source_type="solar_mppt",
    )
    snap = await server.snapshot()
    assert snap["serial_connected"] is False
    assert snap["last_reading_ts"] is None


async def test_snapshot_falls_back_to_state_last_reading_ts() -> None:
    state = HealthState(started_at=time.time(), last_reading_ts=99.0)
    buffer = _make_buffer_mock(total=0, unsynced=0, last_reading=None)
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        state=state,
        buffer=buffer,
        reader=_fake_reader(connected=True),
        hostname_tag="doghouse",
        source_type="solar_mppt",
    )
    snap = await server.snapshot()
    assert snap["last_reading_ts"] == 99.0


async def test_http_endpoint_returns_json(tmp_path: Path) -> None:
    buffer = Buffer(tmp_path / "b.db")
    await buffer.init()
    try:
        await buffer.insert(
            Reading(
                ts_unix=1.0,
                installation_id="bus",
                device_id="mppt",
                raw={"V": "12800"},
                derived={"V": 12.8},
            )
        )
        state = HealthState(started_at=time.time(), last_sync_ts=None)
        server = HealthServer(
            host="127.0.0.1",
            port=0,  # ephemeral
            state=state,
            buffer=buffer,
            reader=_fake_reader(connected=True),
            hostname_tag="doghouse",
            source_type="solar_mppt",
        )
        await server.start()
        try:
            # Resolve the actual bound port from the internal TCPSite.
            assert server._site is not None
            sockets = server._site._server.sockets  # type: ignore[union-attr]
            assert sockets is not None
            port = sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as session, session.get(
                f"http://127.0.0.1:{port}/health"
            ) as resp:
                assert resp.status == 200
                body = json.loads(await resp.text())
            assert body["buffer_total"] == 1
            assert body["buffer_unsynced"] == 1
            assert body["last_reading_ts"] == 1.0
            assert body["serial_connected"] is True
        finally:
            await server.stop()
    finally:
        await buffer.close()


async def test_stop_is_idempotent() -> None:
    state = HealthState(started_at=time.time())
    buffer = _make_buffer_mock(total=0, unsynced=0, last_reading=None)
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        state=state,
        buffer=buffer,
        reader=_fake_reader(connected=False),
        hostname_tag="doghouse",
        source_type="solar_mppt",
    )
    await server.stop()
    await server.stop()
