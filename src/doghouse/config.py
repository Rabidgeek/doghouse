"""Runtime configuration for the DogHouse telemetry logger.

Values load from environment variables, with ``/etc/doghouse/logger.env`` as
the deployed config file and a project-local ``.env`` as a development
fallback. Validation happens at construction time; an invalid config raises
``pydantic.ValidationError`` and the process should fail fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_SHA256_HEX_LEN: Final[int] = 64
_HEX_CHARS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class Settings(BaseSettings):
    """Typed, validated runtime configuration.

    Env vars always win over values pulled from the env files. When both env
    files exist, ``/etc/doghouse/logger.env`` overrides ``.env`` (deployed
    config takes precedence over the developer's local fallback).
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "/etc/doghouse/logger.env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    INSTALLATION_ID: Annotated[str, Field(min_length=1)]
    DEVICE_ID: Annotated[str, Field(min_length=1)]
    SOURCE_TYPE: Annotated[str, Field(min_length=1)] = "solar_mppt"
    HOSTNAME_TAG: Annotated[str, Field(min_length=1)] = "doghouse"

    SERIAL_PORT: Annotated[str, Field(min_length=1)] = "/dev/ttyUSB0"
    SERIAL_TIMEOUT_S: Annotated[float, Field(gt=0)] = 2.0
    SAMPLE_INTERVAL_S: Annotated[float, Field(gt=0)] = 10.0

    BUFFER_DB_PATH: Path = Path("/var/lib/doghouse/buffer.db")

    SYNC_ENDPOINT_URL: AnyHttpUrl
    SYNC_INTERVAL_S: Annotated[float, Field(gt=0)] = 60.0
    SYNC_BATCH_SIZE: Annotated[int, Field(gt=0)] = 500
    SYNC_TIMEOUT_S: Annotated[float, Field(gt=0)] = 30.0
    SYNC_AUTH_TOKEN: str = ""
    SYNC_TLS_FINGERPRINT: str = ""

    HEALTH_PORT: Annotated[int, Field(ge=1, le=65535)] = 9100
    LOG_LEVEL: str = "INFO"

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        """Upper-case and validate the logging level name."""
        if not isinstance(value, str):
            raise TypeError("LOG_LEVEL must be a string")
        upper = value.strip().upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            )
        return upper

    @field_validator("SYNC_TLS_FINGERPRINT", mode="before")
    @classmethod
    def _normalize_fingerprint(cls, value: object) -> str:
        """Accept SHA-256 hex with or without colons; normalize to lower-case hex.

        An empty value disables pinning and is allowed (so dev setups without
        a pinned cert still boot). Any non-empty value must be exactly 64 hex
        digits after stripping colons.
        """
        if value is None or value == "":
            return ""
        if not isinstance(value, str):
            raise TypeError("SYNC_TLS_FINGERPRINT must be a string")
        cleaned = value.replace(":", "").strip().lower()
        if len(cleaned) != _SHA256_HEX_LEN or any(c not in _HEX_CHARS for c in cleaned):
            raise ValueError(
                "SYNC_TLS_FINGERPRINT must be 64 hex chars (SHA-256), "
                "optionally colon-separated"
            )
        return cleaned


def load_settings() -> Settings:
    """Instantiate :class:`Settings` from the environment.

    Separate from the class so callers can inject overrides or pass
    ``_env_file=None`` in tests without touching the class definition.
    """
    return Settings()  # type: ignore[call-arg]
