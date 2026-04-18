# Deploy

Install steps for the Raspberry Pi 4 running Ubuntu Server 24.04 LTS (arm64).

## 1. User + directories

```bash
sudo adduser --system --group --home /opt/doghouse --shell /usr/sbin/nologin doghouse
sudo usermod -aG dialout doghouse
sudo install -d -o doghouse -g doghouse -m 0755 /opt/doghouse
sudo install -d -o doghouse -g doghouse -m 0750 /var/lib/doghouse
sudo install -d -o root -g root -m 0755 /etc/doghouse
```

## 2. Code + virtualenv

```bash
sudo -u doghouse git clone https://github.com/Rabidgeek/DogHouse.git /opt/doghouse
cd /opt/doghouse
sudo -u doghouse uv sync --no-dev
```

`uv sync --no-dev` installs runtime deps only and creates the
`/opt/doghouse/.venv` that the systemd unit's `ExecStart=` points at.

## 3. Config

```bash
sudo install -o root -g doghouse -m 0640 logger.env.example /etc/doghouse/logger.env
sudo $EDITOR /etc/doghouse/logger.env
```

Required values (no defaults):

- `INSTALLATION_ID` — stable identifier for this bus/site
- `DEVICE_ID` — stable identifier for this MPPT
- `SYNC_ENDPOINT_URL` — SOPHIA ingest URL (e.g. `https://sophia:8443/ingest/telemetry`)
- `SYNC_AUTH_TOKEN` — bearer token issued by SOPHIA
- `SYNC_TLS_FINGERPRINT` — SHA-256 hex of SOPHIA's TLS cert (colons optional)

See [`logger.env.example`](logger.env.example) for the full list.

## 4. Systemd

```bash
sudo install -o root -g root -m 0644 deploy/doghouse.service /etc/systemd/system/doghouse.service
sudo systemctl daemon-reload
sudo systemctl enable --now doghouse
```

## 5. Verify

```bash
sudo systemctl status doghouse
journalctl -u doghouse -f      # JSON-per-line structured log
curl -s http://127.0.0.1:9100/health | jq
```

## Upgrades

```bash
sudo -u doghouse git -C /opt/doghouse pull
sudo -u doghouse uv --directory /opt/doghouse sync --no-dev
sudo systemctl restart doghouse
```

## Troubleshooting

- **Serial permission denied** — confirm `doghouse` is in `dialout`:
  `groups doghouse`. Also check `ls -l /dev/ttyUSB0`.
- **TLS fingerprint mismatch** — compute the expected value with
  `openssl s_client -connect sophia:8443 </dev/null | openssl x509 -fingerprint -sha256 -noout`.
- **Buffer growing without sync** — `curl 127.0.0.1:9100/health` and
  look at `last_sync_error`.
