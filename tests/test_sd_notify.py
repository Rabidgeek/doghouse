"""Unit tests for :mod:`doghouse.sd_notify`."""

from __future__ import annotations

import contextlib
import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from doghouse import sd_notify


def test_ready_is_noop_without_notify_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sd_notify.ready()
    sd_notify.watchdog()
    sd_notify.stopping()


def test_watchdog_interval_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sd_notify.watchdog_interval_s() is None


def test_watchdog_interval_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")  # 30 seconds
    assert sd_notify.watchdog_interval_s() == pytest.approx(15.0)


def test_watchdog_interval_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "bogus")
    assert sd_notify.watchdog_interval_s() is None


def test_watchdog_interval_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "0")
    assert sd_notify.watchdog_interval_s() is None


def test_send_delivers_to_unix_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # AF_UNIX paths are capped at ~104 bytes on macOS; tmp_path lives under
    # /var/folders/... which is already close to that limit. Bind under
    # /tmp so the test works regardless of the session's tmp_path length.
    sock_path = Path(tempfile.gettempdir()) / f"dh-notify-{uuid.uuid4().hex[:8]}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(str(sock_path))
        server.settimeout(1.0)
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        sd_notify.ready()
        data, _ = server.recvfrom(1024)
        assert data == b"READY=1"
        sd_notify.watchdog()
        data, _ = server.recvfrom(1024)
        assert data == b"WATCHDOG=1"
    finally:
        server.close()
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
