"""Application wiring: sample loop + sync loop + health server + watchdog.

:func:`run` is the single async entrypoint for the service. It builds
every component, gathers the long-running tasks, and tears everything
down in reverse order on cancellation or crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import TYPE_CHECKING

from doghouse import sd_notify
from doghouse.buffer import Buffer, Reading
from doghouse.health import HealthServer, HealthState
from doghouse.normalize import Normalizer, get_normalizer
from doghouse.sync import HttpPoster, SyncWorker
from doghouse.ve_direct_reader import VEDirectReader

if TYPE_CHECKING:
    from doghouse.config import Settings

_LOG = logging.getLogger(__name__)

_HEALTH_BIND_HOST = "127.0.0.1"


async def run(settings: Settings) -> None:
    """Start every component and block until a signal or crash ends the process.

    Tasks are arranged so that the failure of any one of them cancels
    the others — partial-running services are worse than fast restart
    via systemd.
    """
    # Resolve the normalizer for this source up front — a bad SOURCE_TYPE should
    # crash at startup with a clear message, not silently mis-normalize frames.
    normalizer = get_normalizer(settings.SOURCE_TYPE)

    state = HealthState(started_at=time.time())

    async with contextlib.AsyncExitStack() as stack:
        buffer = Buffer(settings.BUFFER_DB_PATH)
        await buffer.init()
        stack.push_async_callback(buffer.close)

        reader = VEDirectReader(
            port=settings.SERIAL_PORT,
            timeout_s=settings.SERIAL_TIMEOUT_S,
        )
        stack.push_async_callback(reader.close)

        poster = await stack.enter_async_context(
            HttpPoster(
                endpoint_url=str(settings.SYNC_ENDPOINT_URL),
                auth_token=settings.SYNC_AUTH_TOKEN,
                tls_fingerprint_hex=settings.SYNC_TLS_FINGERPRINT,
                hostname_tag=settings.HOSTNAME_TAG,
                source_type=settings.SOURCE_TYPE,
                timeout_s=settings.SYNC_TIMEOUT_S,
            )
        )

        worker = SyncWorker(
            buffer=buffer,
            poster=poster,
            health_state=state,
            interval_s=settings.SYNC_INTERVAL_S,
            batch_size=settings.SYNC_BATCH_SIZE,
        )

        health = HealthServer(
            host=_HEALTH_BIND_HOST,
            port=settings.HEALTH_PORT,
            state=state,
            buffer=buffer,
            reader=reader,
            hostname_tag=settings.HOSTNAME_TAG,
            source_type=settings.SOURCE_TYPE,
        )
        await health.start()
        stack.push_async_callback(health.stop)

        sd_notify.ready()
        stack.callback(sd_notify.stopping)

        _install_shutdown_signals()

        tasks = [
            asyncio.create_task(
                _sample_loop(
                    reader=reader,
                    buffer=buffer,
                    state=state,
                    normalizer=normalizer,
                    installation_id=settings.INSTALLATION_ID,
                    device_id=settings.DEVICE_ID,
                    interval_s=settings.SAMPLE_INTERVAL_S,
                ),
                name="sample-loop",
            ),
            asyncio.create_task(worker.run(), name="sync-loop"),
        ]
        watchdog_task = _maybe_start_watchdog()
        if watchdog_task is not None:
            tasks.append(watchdog_task)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            _LOG.info("shutdown signal received")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _sample_loop(
    *,
    reader: VEDirectReader,
    buffer: Buffer,
    state: HealthState,
    normalizer: Normalizer,
    installation_id: str,
    device_id: str,
    interval_s: float,
) -> None:
    """Read, normalize, and persist one VE.Direct frame per sample interval."""
    _LOG.info("sample loop starting (interval=%.1fs)", interval_s)
    try:
        while True:
            raw = await reader.read_frame()
            ts = time.time()
            derived = normalizer(raw)
            inserted = await buffer.insert(
                Reading(
                    ts_unix=ts,
                    installation_id=installation_id,
                    device_id=device_id,
                    raw=raw,
                    derived=derived,
                )
            )
            if inserted:
                state.last_reading_ts = ts
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOG.exception("sample loop crashed")
        raise


async def _watchdog_loop(interval_s: float) -> None:
    """Ping the systemd watchdog at the configured cadence."""
    _LOG.info("watchdog loop starting (interval=%.1fs)", interval_s)
    try:
        while True:
            sd_notify.watchdog()
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        raise


def _maybe_start_watchdog() -> asyncio.Task[None] | None:
    """Start the watchdog pinger if systemd has set ``WATCHDOG_USEC``."""
    interval = sd_notify.watchdog_interval_s()
    if interval is None:
        return None
    return asyncio.create_task(_watchdog_loop(interval), name="watchdog-loop")


def _install_shutdown_signals() -> None:
    """Route ``SIGINT`` / ``SIGTERM`` to a clean cancellation of the main task.

    When systemd sends ``SIGTERM``, we want the current task to unwind
    through the ``AsyncExitStack``; ``task.cancel()`` is the mechanism.
    """
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    if main_task is None:
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except NotImplementedError:
            # Windows fallback; not relevant on the Pi but harmless.
            _LOG.debug("signal handler for %s not supported on this platform", sig)
