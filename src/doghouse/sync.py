"""Sync worker: batched POST of buffered readings to the SOPHIA ingest endpoint.

The worker polls the buffer, posts a batch, and marks rows synced on 2xx.
Transient failures (5xx, timeout, connection error) drive an exponential
backoff (30 s → 30 min cap, reset on success). Client errors (4xx) are
logged and persisted on each row but do not escalate backoff — 4xx is
typically a persistent misconfiguration, not a load problem.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Self

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType

    from doghouse.buffer import Buffer, BufferedReading
    from doghouse.health import HealthState

_LOG = logging.getLogger(__name__)

_DEFAULT_INITIAL_BACKOFF_S: Final[float] = 30.0
_DEFAULT_MAX_BACKOFF_S: Final[float] = 1800.0  # 30 minutes
_BACKOFF_FACTOR: Final[float] = 2.0
_ERROR_TEXT_MAX_LEN: Final[int] = 500


class SyncResultKind(StrEnum):
    """Outcome categories for a single batch POST attempt."""

    SUCCESS = "success"
    CLIENT_ERROR = "client_error"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of a single attempt, including a human-readable error snippet."""

    kind: SyncResultKind
    error: str | None = None


PostFn = "Callable[[Sequence[BufferedReading]], Awaitable[SyncResult]]"


class HttpPoster:
    """aiohttp-backed implementation of :data:`PostFn`.

    Owns one :class:`aiohttp.ClientSession` for its lifetime. Use as an
    async context manager so the session is closed deterministically.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        auth_token: str,
        tls_fingerprint_hex: str,
        hostname_tag: str,
        source_type: str,
        timeout_s: float,
    ) -> None:
        """Capture the target URL and per-request metadata. No I/O yet."""
        self._endpoint_url = endpoint_url
        self._auth_token = auth_token
        self._hostname_tag = hostname_tag
        self._source_type = source_type
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._ssl: aiohttp.Fingerprint | bool
        if tls_fingerprint_hex:
            self._ssl = aiohttp.Fingerprint(bytes.fromhex(tls_fingerprint_hex))
        else:
            self._ssl = True
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        """Open the underlying HTTP session."""
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP session."""
        del exc_type, exc, tb
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def post(self, readings: Sequence[BufferedReading]) -> SyncResult:
        """POST a batch; classify the outcome."""
        if self._session is None:
            raise RuntimeError("HttpPoster used outside its async context")
        payload = self._build_payload(readings)
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        try:
            async with self._session.post(
                self._endpoint_url,
                json=payload,
                headers=headers,
                ssl=self._ssl,
            ) as resp:
                if 200 <= resp.status < 300:  # noqa: PLR2004
                    return SyncResult(kind=SyncResultKind.SUCCESS)
                body = (await resp.text())[:_ERROR_TEXT_MAX_LEN]
                msg = f"HTTP {resp.status}: {body}".strip()
                if 400 <= resp.status < 500:  # noqa: PLR2004
                    return SyncResult(kind=SyncResultKind.CLIENT_ERROR, error=msg)
                return SyncResult(kind=SyncResultKind.TRANSIENT, error=msg)
        except TimeoutError as exc:
            return SyncResult(kind=SyncResultKind.TRANSIENT, error=f"timeout: {exc}")
        except aiohttp.ClientError as exc:
            return SyncResult(
                kind=SyncResultKind.TRANSIENT,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _build_payload(self, readings: Sequence[BufferedReading]) -> dict[str, Any]:
        return {
            "hostname": self._hostname_tag,
            "source_type": self._source_type,
            "readings": [
                {
                    "ts_unix": r.ts_unix,
                    "installation_id": r.installation_id,
                    "device_id": r.device_id,
                    "raw": r.raw,
                    "derived": r.derived,
                }
                for r in readings
            ],
        }

    async def __call__(self, readings: Sequence[BufferedReading]) -> SyncResult:
        """Allow :class:`HttpPoster` instances to be used as :data:`PostFn`."""
        return await self.post(readings)


class SyncWorker:
    """Periodically drain the buffer's unsynced queue to the ingest endpoint."""

    def __init__(
        self,
        *,
        buffer: Buffer,
        poster: Callable[[Sequence[BufferedReading]], Awaitable[SyncResult]],
        health_state: HealthState,
        interval_s: float,
        batch_size: int,
        initial_backoff_s: float = _DEFAULT_INITIAL_BACKOFF_S,
        max_backoff_s: float = _DEFAULT_MAX_BACKOFF_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Store dependencies and backoff configuration.

        Args:
            buffer: The local SQLite buffer.
            poster: Async callable that posts a batch and returns a result.
            health_state: Shared :class:`HealthState` updated after each tick.
            interval_s: Normal sleep between ticks on success / empty / 4xx.
            batch_size: Max rows per POST.
            initial_backoff_s: First sleep on transient failure.
            max_backoff_s: Cap on transient backoff.
            clock: Time source; overridable in tests.
        """
        if interval_s <= 0 or batch_size <= 0:
            raise ValueError("interval_s and batch_size must be positive")
        if initial_backoff_s <= 0 or max_backoff_s < initial_backoff_s:
            raise ValueError("invalid backoff configuration")
        self._buffer = buffer
        self._poster = poster
        self._state = health_state
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._backoff_s = initial_backoff_s
        self._clock = clock

    @property
    def backoff_s(self) -> float:
        """Current backoff value (for tests and introspection)."""
        return self._backoff_s

    async def run(self) -> None:
        """Run the sync loop until cancelled."""
        _LOG.info("sync worker starting (interval=%.1fs, batch=%d)",
                  self._interval_s, self._batch_size)
        while True:
            sleep_s = await self.tick()
            await asyncio.sleep(sleep_s)

    async def tick(self) -> float:
        """Run one iteration of the loop; return seconds to sleep before next.

        Exposed for tests so they can drive the worker one step at a time
        without dealing with ``asyncio.sleep``.
        """
        rows = await self._buffer.fetch_unsynced(self._batch_size)
        if not rows:
            self._backoff_s = self._initial_backoff_s
            return self._interval_s

        result = await self._poster(rows)
        ids = [r.id for r in rows]

        if result.kind is SyncResultKind.SUCCESS:
            await self._buffer.mark_synced(ids)
            self._state.last_sync_ts = self._clock()
            self._state.last_sync_error = None
            self._backoff_s = self._initial_backoff_s
            _LOG.info("synced %d readings", len(ids))
            return self._interval_s

        err = result.error or "unknown error"
        await self._buffer.mark_failed(ids, err)
        self._state.last_sync_error = err

        if result.kind is SyncResultKind.CLIENT_ERROR:
            _LOG.warning("client error on sync (%d rows): %s", len(ids), err)
            return self._interval_s

        sleep_s = self._backoff_s
        _LOG.warning(
            "transient sync failure (%d rows): %s — backing off %.1fs",
            len(ids), err, sleep_s,
        )
        self._backoff_s = min(self._backoff_s * _BACKOFF_FACTOR, self._max_backoff_s)
        return sleep_s
