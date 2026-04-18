"""Unit tests for :mod:`doghouse.config`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from doghouse.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env: dict[str, str] = {
        "INSTALLATION_ID": "bus_hayfork_01",
        "DEVICE_ID": "mppt_100_50_primary",
        "SYNC_ENDPOINT_URL": "https://sophia:8443/ingest/telemetry",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _build(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_loads_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    s = _build()
    assert s.INSTALLATION_ID == "bus_hayfork_01"
    assert s.DEVICE_ID == "mppt_100_50_primary"
    assert s.SOURCE_TYPE == "solar_mppt"
    assert s.HOSTNAME_TAG == "doghouse"
    assert s.SERIAL_PORT == "/dev/ttyUSB0"
    assert s.SAMPLE_INTERVAL_S == 10.0
    assert str(s.BUFFER_DB_PATH) == "/var/lib/doghouse/buffer.db"
    assert s.SYNC_BATCH_SIZE == 500
    assert s.HEALTH_PORT == 9100
    assert s.LOG_LEVEL == "INFO"


def test_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTALLATION_ID", "x")
    monkeypatch.setenv("DEVICE_ID", "y")
    with pytest.raises(ValidationError):
        _build()


def test_log_level_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LOG_LEVEL="debug")
    assert _build().LOG_LEVEL == "DEBUG"


def test_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LOG_LEVEL="SHOUTY")
    with pytest.raises(ValidationError):
        _build()


def test_fingerprint_empty_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    assert _build().SYNC_TLS_FINGERPRINT == ""


def test_fingerprint_valid_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    fp_colons = ":".join(["ab"] * 32).upper()
    _set_env(monkeypatch, SYNC_TLS_FINGERPRINT=fp_colons)
    assert _build().SYNC_TLS_FINGERPRINT == "ab" * 32


def test_fingerprint_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, SYNC_TLS_FINGERPRINT="deadbeef")
    with pytest.raises(ValidationError):
        _build()


def test_fingerprint_non_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, SYNC_TLS_FINGERPRINT="zz" * 32)
    with pytest.raises(ValidationError):
        _build()


def test_positive_floats_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, SAMPLE_INTERVAL_S="0")
    with pytest.raises(ValidationError):
        _build()


def test_port_range_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, HEALTH_PORT="70000")
    with pytest.raises(ValidationError):
        _build()


def test_invalid_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, SYNC_ENDPOINT_URL="not-a-url")
    with pytest.raises(ValidationError):
        _build()


def test_env_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env vars must take precedence over values read from an env file."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "INSTALLATION_ID=from_file\n"
        "DEVICE_ID=from_file\n"
        "SYNC_ENDPOINT_URL=https://sophia:8443/ingest/telemetry\n"
        "LOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    assert s.INSTALLATION_ID == "from_file"
    assert s.LOG_LEVEL == "DEBUG"
