"""Health server + shared runtime state.

Exposes a single endpoint ``GET /health`` on ``127.0.0.1:<HEALTH_PORT>``
so a local probe (or ``curl`` on the box) can read counters without
opening a firewall port. The server is deliberately tiny — one route,
no auth — because anything reaching ``127.0.0.1`` already has shell
access.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from doghouse.buffer import Buffer
    from doghouse.ve_direct_reader import VEDirectReader

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthState:
    """Mutable snapshot of runtime state updated by worker coroutines.

    ``started_at`` is set once at boot. ``last_sync_ts`` and
    ``last_sync_error`` are mutated by :class:`doghouse.sync.SyncWorker`.
    ``last_reading_ts`` is optionally mutated by the sample loop for a
    cheap in-memory reading timestamp; the authoritative value still
    comes from the buffer.
    """

    started_at: float
    last_sync_ts: float | None = None
    last_sync_error: str | None = None
    last_reading_ts: float | None = None


class HealthServer:
    """aiohttp server that serves ``/health`` as JSON.

    Owns its own runner and site, and cleans both up on :meth:`stop`.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        state: HealthState,
        buffer: Buffer,
        reader: VEDirectReader,
        hostname_tag: str,
        source_type: str,
    ) -> None:
        """Wire the handler to its dependencies. Does not start the server."""
        self._host = host
        self._port = port
        self._state = state
        self._buffer = buffer
        self._reader = reader
        self._hostname_tag = hostname_tag
        self._source_type = source_type
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Bind the listener and begin serving."""
        app = web.Application()
        app.router.add_get("/health", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        _LOG.info("health server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Unbind and free resources. Idempotent."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        """Return the current health payload as JSON."""
        del request
        return web.json_response(await self.snapshot())

    async def snapshot(self) -> dict[str, Any]:
        """Build the health dict. Exposed for tests."""
        counts = await self._buffer.counts()
        last_reading = await self._buffer.last_reading_ts()
        if last_reading is None:
            last_reading = self._state.last_reading_ts
        return {
            "hostname": self._hostname_tag,
            "source_type": self._source_type,
            "uptime_s": int(time.time() - self._state.started_at),
            "buffer_total": counts.total,
            "buffer_unsynced": counts.unsynced,
            "last_reading_ts": last_reading,
            "last_sync_ts": self._state.last_sync_ts,
            "last_sync_error": self._state.last_sync_error,
            "serial_connected": self._reader.connected,
        }
