"""Unit tests for :mod:`doghouse.logging_setup`."""

from __future__ import annotations

import json
import logging

from doghouse import logging_setup


def test_formatter_emits_valid_json() -> None:
    record = logging.LogRecord(
        name="doghouse.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = logging_setup.JsonFormatter().format(record)
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "doghouse.test"
    assert parsed["msg"] == "hello world"


def test_configure_replaces_existing_handlers() -> None:
    root = logging.getLogger()
    placeholder = logging.NullHandler()
    root.addHandler(placeholder)
    try:
        logging_setup.configure("WARNING")
        assert placeholder not in root.handlers
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
