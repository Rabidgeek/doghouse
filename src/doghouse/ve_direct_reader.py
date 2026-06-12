"""Async VE.Direct reader with automatic reconnect on serial errors.

Wraps :class:`vedirect_m8.vedirect.Vedirect` so callers see a single
``await reader.read_frame()`` API. Serial lifecycle (open, close, reopen
after unplug) is owned by this wrapper; consumers never see the raw
``Vedirect`` instance.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any, Final, Protocol

from vedirect_m8.exceptions import VedirectException
from vedirect_m8.vedirect import Vedirect

_LOG = logging.getLogger(__name__)

_INITIAL_BACKOFF_S: Final[float] = 1.0
_MAX_BACKOFF_S: Final[float] = 30.0
_BACKOFF_FACTOR: Final[float] = 2.0


class _VedirectProto(Protocol):
    """Subset of the vedirect-m8 ``Vedirect`` API this module depends on."""

    def read_data_single(self, timeout: int = ...) -> dict[str, Any] | None:
        """Read one decoded VE.Direct block or return ``None`` on timeout."""

    def close_serial(self) -> Any:
        """Close the underlying serial port."""


class VEDirectReaderError(RuntimeError):
    """Raised when the reader is used after :meth:`VEDirectReader.close`."""


def _default_factory(serial_conf: dict[str, Any]) -> _VedirectProto:
    """Construct a real :class:`Vedirect` instance from a serial_conf dict."""
    return Vedirect(serial_conf=serial_conf, auto_start=True)  # type: ignore[no-any-return]


class VEDirectReader:
    """Async, auto-reconnecting reader for a VE.Direct serial stream.

    Attributes:
        port: Serial device path (e.g. ``/dev/ttyUSB0``).
        read_timeout_s: Per-read wall clock budget handed to the underlying
            ``read_data_single`` call. Rounded up to the nearest integer
            second because ``vedirect-m8`` takes an ``int`` timeout.

    The wrapper opens the port lazily on the first :meth:`read_frame` call.
    On any :class:`VedirectException` or :class:`OSError` (cable unplugged,
    port vanished, permission flap), it closes the underlying connection,
    sleeps with bounded exponential backoff, and reopens on the next
    iteration. Backoff resets on the first successful read.
    """

    def __init__(
        self,
        port: str,
        timeout_s: float,
        *,
        initial_backoff_s: float = _INITIAL_BACKOFF_S,
        max_backoff_s: float = _MAX_BACKOFF_S,
        _factory: object = None,
    ) -> None:
        """Store config; do not open the serial port yet.

        Args:
            port: Serial device path.
            timeout_s: Per-read timeout in seconds.
            initial_backoff_s: First sleep duration after a serial error.
            max_backoff_s: Cap on reconnect backoff.
            _factory: Internal hook. When provided, called with the
                ``serial_conf`` dict to build the underlying reader; used
                by tests to inject fakes. Do not rely on externally.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if initial_backoff_s <= 0 or max_backoff_s < initial_backoff_s:
            raise ValueError("invalid backoff configuration")
        self.port: Final[str] = port
        self.read_timeout_s: Final[int] = max(1, math.ceil(timeout_s))
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._backoff_s = initial_backoff_s
        self._vedirect: _VedirectProto | None = None
        self._closed = False
        self._connected = False
        if _factory is None:
            self._factory: Any = _default_factory
        else:
            self._factory = _factory

    @property
    def connected(self) -> bool:
        """True iff the last read succeeded and the port is still open."""
        return self._connected

    async def read_frame(self) -> dict[str, str]:
        """Return the next VE.Direct frame as a raw ``str -> str`` mapping.

        Loops internally until it obtains a frame or the caller cancels.
        Empty/None frames (idle timeouts inside ``vedirect-m8``) are
        transparent to the caller.

        Raises:
            VEDirectReaderError: The reader has been closed.
            asyncio.CancelledError: The surrounding task was cancelled
                while awaiting a read or a backoff sleep.
        """
        if self._closed:
            raise VEDirectReaderError("reader is closed")
        while True:
            try:
                frame = await asyncio.to_thread(self._read_once)
            except (VedirectException, OSError) as exc:
                self._connected = False
                _LOG.warning(
                    "VE.Direct read failed (%s: %s); reconnecting in %.1fs",
                    type(exc).__name__,
                    exc,
                    self._backoff_s,
                )
                await self._disconnect_async()
                await asyncio.sleep(self._backoff_s)
                self._backoff_s = min(self._backoff_s * _BACKOFF_FACTOR, self._max_backoff_s)
                continue
            if frame is None:
                _LOG.debug("empty VE.Direct frame; continuing")
                continue
            self._connected = True
            self._backoff_s = self._initial_backoff_s
            return frame

    def _open_port(self) -> str:
        """Resolve a ``/dev/serial/by-id`` symlink to its real device node.

        ``vedirect-m8`` only accepts ``/dev/ttyUSBN``/``ACMN`` paths
        (``is_unix_serial_port_pattern``) and SILENTLY auto-detects the first
        VE.Direct device for anything else — so handing it a ``by-id`` path makes
        it read whichever cable enumerates first (the wrong one, once a second
        device is attached). We resolve the symlink to the node it points at
        (which vedirect accepts) and re-resolve on every (re)connect, so the
        reader follows THIS cable across USB re-enumeration. If the ``by-id``
        symlink is absent (cable unplugged), ``realpath`` yields a non-device
        path; we raise ``OSError`` so the reconnect backoff handles it — rather
        than letting vedirect auto-detect a DIFFERENT cable. Bare ``/dev/ttyUSBN``
        paths (and test ports) pass through unchanged.
        """
        p = self.port
        if "/by-id/" in p or p.startswith("/dev/serial/"):
            real = str(Path(p).resolve())
            if not (Path(real).exists() and real.startswith("/dev/tty")):
                raise OSError(f"VE.Direct device {p} not present (resolved {real!r})")
            return real
        return p

    def _read_once(self) -> dict[str, str] | None:
        """Synchronous single-frame read. Runs on a worker thread."""
        vedirect = self._vedirect
        if vedirect is None:
            resolved = self._open_port()
            _LOG.info("VE.Direct connecting: %s -> %s", self.port, resolved)
            vedirect = self._factory({"serial_port": resolved})
            self._vedirect = vedirect
        raw = vedirect.read_data_single(timeout=self.read_timeout_s)
        if raw is None:
            return None
        return {str(k): str(v) for k, v in raw.items()}

    async def close(self) -> None:
        """Close the underlying serial port. Idempotent and safe to re-await."""
        self._closed = True
        self._connected = False
        await self._disconnect_async()

    async def _disconnect_async(self) -> None:
        """Close the underlying reader on a worker thread."""
        vedirect, self._vedirect = self._vedirect, None
        if vedirect is None:
            return

        def _close() -> None:
            try:
                vedirect.close_serial()
            except Exception:
                _LOG.debug("close_serial raised during teardown", exc_info=True)

        await asyncio.to_thread(_close)
