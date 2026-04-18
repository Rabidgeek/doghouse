# DogHouse

VE.Direct solar telemetry edge node. Runs on a Raspberry Pi 4 mounted on a bus,
reads a Victron SmartSolar MPPT 100|50 over VE.Direct USB, buffers locally in
SQLite (WAL), and forwards batches to the SOPHIA ingest endpoint over Tailscale.

## Hardware

- Raspberry Pi 4 (4 GB) running Ubuntu Server 24.04 LTS (arm64)
- Victron SmartSolar MPPT 100|50
- VE.Direct USB cable (FTDI FT232) on `/dev/ttyUSB0`
- 12 V 100 Ah LiFePO4 pack
- Intermittent WiFi / cell tether; Tailscale always on

## Stack

- Python 3.12, managed with [uv](https://github.com/astral-sh/uv)
- `aiosqlite`, `aiohttp`, `vedirect-m8`, `pydantic-settings`
- `ruff`, `mypy --strict`, `pytest`, `pytest-asyncio`
- Structured JSON logging to stdout, consumed by `journald`

## Development

```bash
uv sync
uv run ruff check .
uv run mypy
uv run pytest
```

## Layout

```
src/doghouse/     # application package
tests/            # unit + integration tests
```

## Configuration

Runtime config loads from `/etc/doghouse/logger.env` with a local `.env`
fallback for development. See the project spec for the full variable list.

## Deployment

Installed as a systemd unit with `WatchdogSec` and `sd_notify` keep-alives.
Unit file and install instructions land in `deploy/` in a later scope item.
