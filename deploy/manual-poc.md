# Manual PoC — one box, by hand (~30 min, throwaway)

The minimum to see Elenchus serving over HTTPS on a real domain from a
persistent box. **Synthetic data only, no participants** (no DPO gate).
Deliberately skips the production apparatus — separate data volume, S3
backups, alarms, SSM, IAM, secrets manager. Those live in
[`cloud-deployment.md`](../docs/cloud-deployment.md) /
[`OPERATIONS.md`](../docs/OPERATIONS.md) for later, on whatever substrate
you actually launch on (likely SURF). The on-box steps below are the same
on a SURF VM, so this transfers.

Assumes `elenchus.chat` is in Route 53 and you have an Anthropic API key.

## 1. Get a box

Easiest: **AWS Lightsail** → create instance → Ubuntu 24.04 → smallest
plan with ≥1 GB RAM (~$7/mo) → attach a **static IP** → in *Networking*,
allow **80** and **443** (22 is open by default). (A default-VPC EC2
`t3.small` with a security group allowing 22/80/443 works identically.)

## 2. Point DNS

Route 53 → hosted zone `elenchus.chat` → create an **A** record:
`poc.elenchus.chat` → the box's static IP, TTL 60. Do this now so DNS has
propagated before step 5.

## 3. Install + run the app

SSH in, then:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv nginx certbot python3-certbot-nginx

# dedicated user + venv (matches OPERATIONS.md; transfers to SURF)
sudo useradd --system --create-home --home /opt/elenchus --shell /usr/sbin/nologin elenchus
sudo -u elenchus python3 -m venv /opt/elenchus/venv
sudo -u elenchus /opt/elenchus/venv/bin/pip install --upgrade pip "elenchus>=0.2.0"
sudo mkdir -p /var/lib/elenchus && sudo chown elenchus:elenchus /var/lib/elenchus

# env file (root-owned, group-readable; not world-readable)
sudo mkdir -p /etc/elenchus
sudo tee /etc/elenchus/elenchus.env >/dev/null <<'ENV'
ELENCHUS_API_KEY=sk-ant-REPLACE_ME
ELENCHUS_DATA=/var/lib/elenchus
ELENCHUS_MODEL=claude-sonnet-4-6
PORT=8741
SESSION_COOKIE_SECURE=true
BCRYPT_ROUNDS=12
# Master key so an admin can set/rotate the LLM API key from the UI and
# have it persist (encrypted) across restarts. Generate once:
ELENCHUS_SECRET_KEY=$(openssl rand -base64 36)
ENV
sudo sed -i "s|sk-ant-REPLACE_ME|<your real key>|" /etc/elenchus/elenchus.env
sudo chgrp elenchus /etc/elenchus/elenchus.env && sudo chmod 640 /etc/elenchus/elenchus.env

# bootstrap the admin BEFORE starting the server (single-writer)
sudo -u elenchus env ELENCHUS_DATA=/var/lib/elenchus BCRYPT_ROUNDS=12 \
  ELENCHUS_ADMIN_PASSWORD='<choose-a-password>' \
  /opt/elenchus/venv/bin/elenchus admin create --email admin@local --name "PoC Admin"
```

systemd unit (condensed from OPERATIONS.md §4):

```bash
sudo tee /etc/systemd/system/elenchus.service >/dev/null <<'UNIT'
[Unit]
Description=Elenchus server (PoC)
After=network-online.target
Wants=network-online.target
[Service]
User=elenchus
Group=elenchus
EnvironmentFile=/etc/elenchus/elenchus.env
ExecStart=/opt/elenchus/venv/bin/elenchus
Restart=on-failure
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now elenchus
curl -sf http://127.0.0.1:8741/healthz   # local sanity check
```

## 4. Reverse proxy

```bash
sudo tee /etc/nginx/sites-available/elenchus >/dev/null <<'NGINX'
server {
    listen 80;
    server_name poc.elenchus.chat;
    location / {
        proxy_pass http://127.0.0.1:8741;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;        # LLM turns can take ~30s
        client_max_body_size 5m;
    }
}
NGINX
sudo ln -sf /etc/nginx/sites-available/elenchus /etc/nginx/sites-enabled/elenchus
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 5. TLS

```bash
sudo certbot --nginx -d poc.elenchus.chat -m b.p.allen@uva.nl --agree-tos --redirect -n
```

certbot does HTTP-01 over port 80 (no IAM/Route 53 plumbing needed),
rewrites the Nginx block for 443 + an 80→443 redirect, and installs an
auto-renew timer.

## 6. Verify

```bash
curl -sf https://poc.elenchus.chat/healthz | jq
#  {"status":"ok","phase_b_enabled":false,"llm_configured":true,...}
```

Open `https://poc.elenchus.chat/`, sign in as `admin@local`, run a
dialectic — or point a synthetic run at it. **No real participants.**

## 7. Tear down

Delete the Lightsail instance + its static IP, and remove the
`poc.elenchus.chat` A record in Route 53. Done.
