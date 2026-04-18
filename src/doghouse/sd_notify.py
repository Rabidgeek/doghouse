"""Minimal sd_notify client for systemd ``Type=notify`` integration.

Deliberately tiny: no dependency on the ``systemd`` Python package,
no parsing of ``$NOTIFY_SOCKET`` beyond abstract-namespace handling.
All functions are no-ops when not running under systemd (i.e. the
``NOTIFY_SOCKET`` environment variable is absent), so they are safe to
call from the app in local development.
"""

from __future__ import annotations

import logging
import os
import socket

_LOG = logging.getLogger(__name__)


def _resolve_address() -> str | bytes | None:
    """Return the systemd notify socket address, or ``None`` if unset."""
    raw = os.environ.get("NOTIFY_SOCKET")
    if not raw:
        return None
    if raw.startswith("@"):
        # Linux abstract namespace: leading nul byte, rest is the name.
        return "\0" + raw[1:]
    return raw


def _send(message: str) -> None:
    """Send a single notify message; swallow and log any error."""
    addr = _resolve_address()
    if addr is None:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
    except OSError as exc:
        _LOG.debug("sd_notify send failed: %s", exc)


def ready() -> None:
    """Notify systemd that the service has finished starting up."""
    _send("READY=1")


def watchdog() -> None:
    """Ping the systemd watchdog to reset its timer."""
    _send("WATCHDOG=1")


def stopping() -> None:
    """Notify systemd that the service is shutting down cleanly."""
    _send("STOPPING=1")


def watchdog_interval_s() -> float | None:
    """Return ``WATCHDOG_USEC / 2`` in seconds, or ``None`` if unset.

    systemd's recommendation is to ping at half the configured
    ``WatchdogSec``. Returning ``None`` signals "no watchdog configured"
    so callers can skip the ping loop entirely.
    """
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        _LOG.warning("invalid WATCHDOG_USEC=%r", raw)
        return None
    if usec <= 0:
        return None
    return (usec / 1_000_000.0) / 2.0
