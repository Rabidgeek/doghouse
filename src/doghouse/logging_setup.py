"""Structured JSON logging to stdout for ``journald``/journalctl consumers.

One record per line, stable field names, exception info folded in as a
``stack`` string so tooling doesn't have to parse multi-line log frames.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    """Serialize :class:`logging.LogRecord` instances as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a JSON line."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure(level: str) -> None:
    """Install the JSON handler on the root logger.

    Idempotent: existing handlers on the root logger are replaced so
    repeated invocations (e.g. in tests) do not stack up duplicates.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
