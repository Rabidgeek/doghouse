"""End-to-end integration: socat PTY pair + vedirectsim → reader → buffer.

Skipped automatically when ``socat`` is not on ``PATH``.

The vedirect-m8 library only accepts serial paths that are either under
``/dev`` or named ``vmodem<0-999>`` inside the home directory (see
``SerialConnection._get_virtual_ports_paths``). To stay inside those
rules without touching the real ``$HOME``, the fixture points ``HOME``
at a pytest-managed tmp dir for the duration of the test and creates
``vmodemA`` / ``vmodemB`` symlinks there via socat.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from doghouse.buffer import Buffer, Reading
from doghouse.normalize import normalize_mppt
from doghouse.ve_direct_reader import VEDirectReader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("socat") is None,
        reason="socat not installed; end-to-end test requires it",
    ),
]

_SOCAT_READY_TIMEOUT_S = 5.0
_FRAME_TIMEOUT_S = 20.0


@contextlib.contextmanager
def _socat_pty_pair(home_dir: Path) -> Iterator[tuple[Path, Path]]:
    """Spawn socat creating two PTY symlinks under ``home_dir``.

    Yields ``(writer_side, reader_side)`` paths named to satisfy
    vedirect-m8's ``vmodem<n>`` home-directory regex.
    """
    side_a = home_dir / "vmodem100"
    side_b = home_dir / "vmodem101"
    socat = shutil.which("socat")
    assert socat is not None
    proc = subprocess.Popen(  # noqa: S603
        [
            socat,
            "-d",
            "-d",
            f"pty,raw,echo=0,link={side_a}",
            f"pty,raw,echo=0,link={side_b}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + _SOCAT_READY_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if side_a.exists() and side_b.exists():
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("socat did not create PTY symlinks in time")
        yield side_a, side_b
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


@contextlib.contextmanager
def _vedirectsim_process(
    port: Path, home_dir: Path
) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn vedirect-m8's built-in simulator writing to ``port``.

    Runs in a child Python so its serial loop can block without
    blocking the asyncio test loop. ``HOME`` is overridden so the
    simulator sees the PTY symlink under the same virtual-port path
    the library enforces.
    """
    script = (
        "from vedirect_m8.vedirectsim import Vedirectsim;"
        f"Vedirectsim(serialport={str(port)!r}, device='smartsolar_1.39').run()"
    )
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        yield proc
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


async def test_end_to_end_read_normalize_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    buffer = Buffer(tmp_path / "buffer.db")
    await buffer.init()
    try:
        with _socat_pty_pair(home) as (writer_pty, reader_pty), _vedirectsim_process(
            writer_pty, home
        ):
            reader = VEDirectReader(
                port=str(reader_pty),
                timeout_s=5.0,
                initial_backoff_s=0.1,
                max_backoff_s=0.5,
            )
            try:
                frame = await asyncio.wait_for(
                    reader.read_frame(), timeout=_FRAME_TIMEOUT_S
                )
            finally:
                await reader.close()
        derived = normalize_mppt(frame)
        # SmartSolar 1.39 simulator emits V, VPV, PPV, I, CS, MPPT, ERR.
        assert "V" in derived
        assert "charge_state_name" in derived
        assert "mppt_state_name" in derived
        inserted = await buffer.insert(
            Reading(
                ts_unix=1.0,
                installation_id="test",
                device_id="test",
                raw=frame,
                derived=derived,
            )
        )
        assert inserted is True
        [row] = await buffer.fetch_unsynced(limit=1)
        assert row.raw == frame
        assert row.derived["charge_state_name"] == derived["charge_state_name"]
    finally:
        await buffer.close()
