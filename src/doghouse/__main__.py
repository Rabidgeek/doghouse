"""CLI entrypoint: ``python -m doghouse`` and the installed ``doghouse`` script.

The script is intentionally thin; anything more than config loading and
delegation to :func:`doghouse.app.run` belongs elsewhere so the service
shape stays testable.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pydantic import ValidationError

from doghouse.app import run
from doghouse.config import load_settings
from doghouse.logging_setup import configure as configure_logging

_LOG = logging.getLogger(__name__)


def main() -> int:
    """Load config, set up logging, and run the async app.

    Returns a POSIX exit code; 0 on clean shutdown, 1 on config error,
    2 on unexpected crash.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        # Logging isn't configured yet — write to stderr so systemd captures it.
        print(f"doghouse: invalid configuration:\n{exc}", file=sys.stderr)
        return 1

    configure_logging(settings.LOG_LEVEL)
    _LOG.info(
        "starting doghouse (installation=%s device=%s)",
        settings.INSTALLATION_ID,
        settings.DEVICE_ID,
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        _LOG.info("interrupted; exiting")
    except Exception:
        _LOG.exception("doghouse crashed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
