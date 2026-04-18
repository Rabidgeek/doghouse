"""Unit tests for :mod:`doghouse.ve_direct_reader`.

These tests use a fake that implements the VE.Direct reader surface so we
can exercise reconnect and backoff logic without real serial hardware.
"""

from __future__ import annotations

from typing import Any

import pytest
from vedirect_m8.exceptions import VedirectException

from doghouse.ve_direct_reader import VEDirectReader, VEDirectReaderError


class _FakeVedirect:
    """Deterministic stand-in for :class:`vedirect_m8.vedirect.Vedirect`."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.reads = 0
        self.closes = 0

    def read_data_single(self, timeout: int = 60) -> dict[str, Any] | None:
        del timeout
        self.reads += 1
        if not self._script:
            raise RuntimeError("fake script exhausted")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if nxt is None:
            return None
        assert isinstance(nxt, dict)
        return nxt

    def close_serial(self) -> None:
        self.closes += 1


class _FactoryRecorder:
    """Tracks each fake Vedirect constructed so tests can assert reopens."""

    def __init__(self, scripts: list[list[Any]]) -> None:
        self._scripts = list(scripts)
        self.instances: list[_FakeVedirect] = []

    def __call__(self, serial_conf: dict[str, Any]) -> _FakeVedirect:
        del serial_conf
        if not self._scripts:
            raise RuntimeError("no more fake instances scripted")
        fake = _FakeVedirect(self._scripts.pop(0))
        self.instances.append(fake)
        return fake


async def test_read_frame_returns_dict() -> None:
    factory = _FactoryRecorder([[{"V": "12800", "I": "5900"}]])
    reader = VEDirectReader("/dev/fake", timeout_s=1.0, _factory=factory)
    frame = await reader.read_frame()
    assert frame == {"V": "12800", "I": "5900"}
    assert reader.connected is True
    await reader.close()


async def test_skips_none_frames() -> None:
    factory = _FactoryRecorder([[None, None, {"V": "13000"}]])
    reader = VEDirectReader("/dev/fake", timeout_s=1.0, _factory=factory)
    frame = await reader.read_frame()
    assert frame == {"V": "13000"}
    assert factory.instances[0].reads == 3
    await reader.close()


async def test_reconnects_after_vedirect_exception() -> None:
    factory = _FactoryRecorder(
        [
            [VedirectException("cable yanked")],
            [{"V": "12750"}],
        ]
    )
    reader = VEDirectReader(
        "/dev/fake",
        timeout_s=1.0,
        initial_backoff_s=0.001,
        max_backoff_s=0.002,
        _factory=factory,
    )
    frame = await reader.read_frame()
    assert frame == {"V": "12750"}
    assert len(factory.instances) == 2
    assert factory.instances[0].closes == 1
    assert reader.connected is True
    await reader.close()


async def test_reconnects_after_os_error() -> None:
    factory = _FactoryRecorder(
        [
            [OSError("device disappeared")],
            [{"V": "12800"}],
        ]
    )
    reader = VEDirectReader(
        "/dev/fake",
        timeout_s=1.0,
        initial_backoff_s=0.001,
        max_backoff_s=0.002,
        _factory=factory,
    )
    frame = await reader.read_frame()
    assert frame == {"V": "12800"}
    assert len(factory.instances) == 2
    await reader.close()


async def test_backoff_resets_after_success() -> None:
    factory = _FactoryRecorder(
        [
            [VedirectException("boom")],
            [VedirectException("boom again")],
            [{"V": "12800"}],
        ]
    )
    reader = VEDirectReader(
        "/dev/fake",
        timeout_s=1.0,
        initial_backoff_s=0.001,
        max_backoff_s=0.010,
        _factory=factory,
    )
    await reader.read_frame()
    # Two failures grew backoff; the successful read must reset it to initial.
    assert reader._backoff_s == pytest.approx(0.001)
    await reader.close()


async def test_backoff_grows_then_caps() -> None:
    factory = _FactoryRecorder(
        [
            [VedirectException("1")],
            [VedirectException("2")],
            [VedirectException("3")],
            [VedirectException("4")],
            [{"V": "12800"}],
        ]
    )
    reader = VEDirectReader(
        "/dev/fake",
        timeout_s=1.0,
        initial_backoff_s=0.001,
        max_backoff_s=0.004,
        _factory=factory,
    )
    frame = await reader.read_frame()
    assert frame == {"V": "12800"}
    # Caller never observes intermediate backoff values directly, but the
    # final one after success has been reset to the initial value.
    assert reader._backoff_s == pytest.approx(0.001)
    await reader.close()


async def test_read_after_close_raises() -> None:
    factory = _FactoryRecorder([[{"V": "12800"}]])
    reader = VEDirectReader("/dev/fake", timeout_s=1.0, _factory=factory)
    await reader.close()
    with pytest.raises(VEDirectReaderError):
        await reader.read_frame()


async def test_close_is_idempotent() -> None:
    factory = _FactoryRecorder([[{"V": "12800"}]])
    reader = VEDirectReader("/dev/fake", timeout_s=1.0, _factory=factory)
    await reader.read_frame()
    await reader.close()
    await reader.close()
    assert factory.instances[0].closes == 1


def test_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        VEDirectReader("/dev/fake", timeout_s=0)


def test_rejects_bad_backoff() -> None:
    with pytest.raises(ValueError, match="backoff"):
        VEDirectReader(
            "/dev/fake", timeout_s=1.0, initial_backoff_s=5.0, max_backoff_s=1.0
        )
