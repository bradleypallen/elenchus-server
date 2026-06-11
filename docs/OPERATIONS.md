# Operations Runbook

This document covers deploying and operating an Elenchus server in
production — the configuration the Sloan study pilot runs on. It
assumes a single small Linux VM (2 vCPU / 4 GB RAM is ample for ≤24
participants) with a public hostname and TLS.

> **DuckDB is single-writer-per-file.** Elenchus runs as **one**
> server process. Do not run multiple workers (`uvicorn --workers N`)
> or multiple containers against the same data directory — they will
> corrupt each other. Vertical scale only; horizontal scale means
> migrating the platform DB to Postgres (the `db/registry.py`
> boundary is shaped for that swap).

## 1. Install

```bash
# As a dedicated unprivileged user, e.g. `elenchus`.
sudo useradd --system --create-home --shell /usr/sbin/nologin elenchus
sudo -u elenchus -H bash
cd ~
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install elenchus    # or: pip install -e . from a checkout
```

The data directory (`$ELENCHUS_DATA`, default `./dialectics`) holds
everything stateful: `platform.duckdb`, `bases/{actor_id}/*.duckdb`,
`backups/`, and `exports/`. Put it on persistent, backed-up storage.

```bash
sudo -u elenchus mkdir -p /var/lib/elenchus
```

## 2. Environment

Create `/etc/elenchus/elenchus.env` (root-owned, mode 0640, group
`elenchus`) — never commit secrets to the repo:

```ini
# ── Required ──
ELENCHUS_API_KEY=sk-ant-...          # or ANTHROPIC_API_KEY
ELENCHUS_DATA=/var/lib/elenchus
PORT=8741

# ── Recommended in production ──
ELENCHUS_MODEL=claude-opus-4-6
SESSION_COOKIE_SECURE=true           # required behind HTTPS
BCRYPT_ROUNDS=12                     # production default; never lower

# ── Sloan study: leave Phase B OFF ──
# (omit ELENCHUS_ENABLE_PHASE_B entirely — default is off)

# ── Alerting (Phase C) ──
ALERT_EMAIL_TO=ops@your-institution.edu
ALERT_EMAIL_MIN_SEVERITY=high
EMAIL_BACKEND=smtp
SMTP_HOST=smtp.your-institution.edu
SMTP_PORT=587
SMTP_USER=elenchus@your-institution.edu
SMTP_PASSWORD=...
SMTP_FROM=elenchus@your-institution.edu

# ── Cost tracking (optional override of default rates) ──
# ELENCHUS_PRICING_JSON={"claude-opus-4-6":{"input_per_1m":15,"output_per_1m":75}}

# ── Backup cron auth ──
ELENCHUS_BACKUP_EMAIL=admin@local
ELENCHUS_BACKUP_PASSWORD=...
ELENCHUS_URL=http://localhost:8741
```

## 3. Bootstrap the first admin

One-time, before first launch:

```bash
sudo -u elenchus env $(grep -v '^#' /etc/elenchus/elenchus.env | xargs) \
  /home/elenchus/venv/bin/elenchus admin create \
  --email admin@local --name "Study Admin"
```

(Or set `ELENCHUS_ADMIN_PASSWORD` in the env file for a non-interactive
create.) Migrating from a pre-0.2 single-user install? Run
`elenchus migrate-legacy --create-admin` once.

## 4. systemd unit

`/etc/systemd/system/elenchus.service`:

```ini
[Unit]
Description=Elenchus dialectical knowledge base server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=elenchus
Group=elenchus
EnvironmentFile=/etc/elenchus/elenchus.env
ExecStart=/home/elenchus/venv/bin/elenchus
Restart=on-failure
RestartSec=5

# Graceful shutdown: the FastAPI lifespan handler closes every DuckDB
# connection (flushing WALs) on SIGTERM. Give it room.
TimeoutStopSec=30
KillSignal=SIGTERM

# Raise the file-descriptor limit — each open base costs ≥1 fd; the
# registry warns below 256.
LimitNOFILE=4096

# Hardening.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/elenchus
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now elenchus
sudo systemctl status elenchus
journalctl -u elenchus -f         # live logs
```

## 5. Nginx (TLS termination + reverse proxy)

Elenchus serves plain HTTP; terminate TLS at Nginx. With
`SESSION_COOKIE_SECURE=true`, cookies are only sent over HTTPS, so the
proxy **must** set `X-Forwarded-Proto`.

`/etc/nginx/sites-available/elenchus`:

```nginx
server {
    listen 443 ssl http2;
    server_name elenchus.your-institution.edu;

    ssl_certificate     /etc/letsencrypt/live/elenchus.your-institution.edu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/elenchus.your-institution.edu/privkey.pem;

    # LLM turns can take 30 s; don't let the proxy time them out.
    proxy_read_timeout 120s;
    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:8741;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name elenchus.your-institution.edu;
    return 301 https://$host$request_uri;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/elenchus /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# TLS cert (one-time): sudo certbot --nginx -d elenchus.your-institution.edu
```

## 6. Health monitoring

`GET /healthz` is unauthenticated and cheap (one platform-DB read; no
per-base files touched). It returns `200 {"status":"ok",...}` when
healthy and `503 {"status":"degraded",...}` when the platform DB is
unreachable or the data dir isn't writable. Point your uptime monitor
at it:

```bash
curl -sf https://elenchus.your-institution.edu/healthz | jq
# {"status":"ok","schema_version":7,"phase_b_enabled":false,
#  "llm_configured":true,"checks":{"platform_db":"ok","data_dir":"ok"}}
```

The response also surfaces two config sanity flags worth alerting on:
`phase_b_enabled` (must be `false` for the Sloan study) and
`llm_configured` (must be `true`, or every dialogue turn will fail).

## 7. Backups

`scripts/backup.py` calls `POST /api/admin/backup` over HTTP, so the
backup runs *inside* the server process under DuckDB's locks (a
parallel process opening the files would conflict). Daily cron:

```cron
# /etc/cron.d/elenchus-backup  —  03:00 daily, keep 14 archives
0 3 * * * elenchus . /etc/elenchus/elenchus.env; /home/elenchus/venv/bin/python /home/elenchus/scripts/backup.py --keep 14 >> /var/log/elenchus/backup.log 2>&1
```

Archives land in `$ELENCHUS_DATA/backups/elenchus-*.tar.gz` (DuckDB
`EXPORT DATABASE` dumps — restorable with `IMPORT DATABASE`). Copy
them off-box (object storage) for durability; the cron only handles
local retention.

**Restore** (manual, server stopped): extract an archive and
`IMPORT DATABASE 'path/'` into a fresh DuckDB file per the export
layout. Restoring is the rollback path — migrations are forward-only.

## 8. Per-study data export

At the end of a study, a researcher runs the export from the
dashboard (Study tab → EXPORT) or via the API:

```bash
curl -sf -b cookies.txt -X POST \
  https://elenchus.your-institution.edu/api/admin/study/PILOT/export
```

This writes `$ELENCHUS_DATA/exports/study-PILOT-*.tar.gz` (the
analysis-ready archive with pseudonymized IDs) plus a
`*.pseudonyms.json` mapping file **next to** the archive. The mapping
file links opaque IDs back to real participants — keep it with your
participant-tracking records and **exclude it from any public
deposit** (Zenodo, OSF). The tar itself carries no emails or names.

## 9. Log rotation

systemd-journald already rotates `journalctl` output. The cron-side
backup log needs its own rotation — `/etc/logrotate.d/elenchus`:

```
/var/log/elenchus/*.log {
    weekly
    rotate 12
    compress
    missingok
    notifempty
    create 0640 elenchus elenchus
}
```

```bash
sudo mkdir -p /var/log/elenchus && sudo chown elenchus:elenchus /var/log/elenchus
```

## 10. Operational checklist (Sloan pilot)

Before the first participant session:

- [ ] `elenchus sim` (scripted) passes — the full study flow runs green
      end-to-end through every role, **including the access/auth probe
      phase** (tenant isolation, privilege gating, single-use tokens,
      session revocation, judge-view blinding). Then `elenchus sim
      --driver llm` against the production model as a dress rehearsal:
      confirm real participants/judges complete, check the cost + p95
      latency the report prints, and sanity-check that judge
      condition-guess accuracy is near chance (the blinding holds).
- [ ] `RUN_UI_E2E=1 pytest tests/e2e/` passes — the real frontend
      renders and routes in a browser (login, signup-from-invite,
      participant study link, blinded judge view, graceful auth errors).
      Requires `pip install -e ".[e2e]"` + `python -m playwright install
      chromium`. See `tests/e2e/README.md`.
- [ ] `GET /healthz` returns 200 with `llm_configured:true` and
      `phase_b_enabled:false`.
- [ ] Admin can log in; a test participant token consumes cleanly and
      walks briefing → tutorial → active.
- [ ] `EMAIL_BACKEND=smtp` and a test invite email actually arrives
      (the alerting + invite paths share the SMTP backend).
- [ ] `ALERT_EMAIL_TO` set; trigger a simulated failure (e.g. revoke
      the API key for one turn) and confirm an alert email lands.
- [ ] Backup cron has run once and produced a readable archive.
- [ ] TLS valid; `SESSION_COOKIE_SECURE=true`; cookies flow.
- [ ] EEQ questionnaire wording reviewed and signed off (the eight
      items in `questionnaires.py` ship as a draft). Review packet with
      per-item construct/concern analysis + a decision sheet:
      [`docs/eeq-review.md`](eeq-review.md). Any reword → bump
      `INSTRUMENT_VERSION`.
