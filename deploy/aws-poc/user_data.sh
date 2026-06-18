#!/usr/bin/env bash
# Elenchus AWS PoC bootstrap. STATIC (not a Terraform template) — it reads
# its config + secrets from SSM Parameter Store at boot, so there is no
# template interpolation to escape. SSM_PREFIX must match var.ssm_prefix.
#
# Idempotent-ish: re-running (instance replacement) re-uses the existing
# data volume without reformatting it.
set -euxo pipefail
exec > >(tee -a /var/log/elenchus-bootstrap.log) 2>&1

SSM_PREFIX="/elenchus/poc"
MOUNT="/var/lib/elenchus"
VENV="/opt/elenchus/venv"
ENV_FILE="/etc/elenchus/elenchus.env"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-dev build-essential nginx \
  certbot python3-certbot-dns-route53 awscli curl ca-certificates

# ── Region + config/secrets from SSM (via the instance role, IMDSv2) ────
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)

getp()  { aws ssm get-parameter --region "$REGION" --name "$1" \
            --query 'Parameter.Value' --output text; }
getps() { aws ssm get-parameter --region "$REGION" --name "$1" --with-decryption \
            --query 'Parameter.Value' --output text; }

HOSTNAME_FQDN=$(getp  "$SSM_PREFIX/hostname")
LE_EMAIL=$(getp       "$SSM_PREFIX/le_email")
MODEL=$(getp          "$SSM_PREFIX/model")
S3_BUCKET=$(getp      "$SSM_PREFIX/s3_bucket")
PACKAGE=$(getp        "$SSM_PREFIX/package")
API_KEY=$(getps       "$SSM_PREFIX/anthropic_api_key")
ADMIN_PW=$(getps      "$SSM_PREFIX/admin_password")

if [ -z "$API_KEY" ] || [ "$API_KEY" = "None" ] || [ -z "$ADMIN_PW" ] || [ "$ADMIN_PW" = "None" ]; then
  echo "FATAL: set $SSM_PREFIX/anthropic_api_key and $SSM_PREFIX/admin_password (SecureString) before boot." >&2
  exit 1
fi

# ── Mount the data volume (the disk that is not the root disk) ──────────
mkdir -p "$MOUNT"
ROOTPART=$(findmnt -no SOURCE /)
ROOTDISK=$(lsblk -no pkname "$ROOTPART" | head -1)
DATA_DEV=""
for _ in $(seq 1 60); do
  DATA_DEV=$(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}' | grep -v "^/dev/$ROOTDISK$" | head -1 || true)
  [ -n "$DATA_DEV" ] && break
  sleep 5
done
[ -n "$DATA_DEV" ] || { echo "FATAL: data volume not found" >&2; exit 1; }
if [ -z "$(lsblk -no FSTYPE "$DATA_DEV" | tr -d '[:space:]')" ]; then
  mkfs.ext4 -F "$DATA_DEV"   # first boot only — never reformats existing data
fi
UUID=$(blkid -s UUID -o value "$DATA_DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $MOUNT ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a

# ── App user + venv + install ───────────────────────────────────────────
id elenchus &>/dev/null || useradd --system --home /opt/elenchus --shell /usr/sbin/nologin elenchus
mkdir -p /opt/elenchus
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install "$PACKAGE"
chown -R elenchus:elenchus /opt/elenchus "$MOUNT"

# ── Env file (root:elenchus 0640; no admin password persisted here) ─────
mkdir -p /etc/elenchus
cat > "$ENV_FILE" <<ENV
ELENCHUS_API_KEY=$API_KEY
ELENCHUS_DATA=$MOUNT
ELENCHUS_MODEL=$MODEL
PORT=8741
SESSION_COOKIE_SECURE=true
BCRYPT_ROUNDS=12
ENV
chown root:elenchus "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# ── Bootstrap the admin BEFORE the server starts (single-writer) ────────
sudo -u elenchus env ELENCHUS_DATA="$MOUNT" BCRYPT_ROUNDS=12 ELENCHUS_ADMIN_PASSWORD="$ADMIN_PW" \
  "$VENV/bin/elenchus" admin create --email admin@local --name "PoC Admin" || true

# ── systemd unit (mirrors docs/OPERATIONS.md) ───────────────────────────
cat > /etc/systemd/system/elenchus.service <<'UNIT'
[Unit]
Description=Elenchus dialectical knowledge base server (PoC)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=elenchus
Group=elenchus
EnvironmentFile=/etc/elenchus/elenchus.env
ExecStart=/opt/elenchus/venv/bin/elenchus
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
LimitNOFILE=4096
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/elenchus
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now elenchus

# ── TLS via certbot dns-route53 (uses the instance role; no port-80 ACME) ─
certbot certonly --dns-route53 -d "$HOSTNAME_FQDN" \
  --non-interactive --agree-tos -m "$LE_EMAIL"
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env bash
systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# ── Nginx reverse proxy (HTTPS → 127.0.0.1:8741; long LLM-turn timeout) ─
# $HOSTNAME_FQDN expands here; nginx runtime vars ($host, $scheme, …) are
# escaped so they stay literal.
cat > /etc/nginx/sites-available/elenchus <<NGINX
server {
    listen 80;
    server_name $HOSTNAME_FQDN;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl http2;
    server_name $HOSTNAME_FQDN;

    ssl_certificate     /etc/letsencrypt/live/$HOSTNAME_FQDN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$HOSTNAME_FQDN/privkey.pem;

    proxy_read_timeout 120s;      # LLM turns can take ~30s
    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:8741;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/elenchus /etc/nginx/sites-enabled/elenchus
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ── Backup: EXPORT DATABASE via the admin endpoint → S3, on a daily timer ─
cat > /usr/local/bin/elenchus-backup.sh <<'BACKUP'
#!/usr/bin/env bash
set -euo pipefail
SSM_PREFIX="/elenchus/poc"
MOUNT="/var/lib/elenchus"
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
S3_BUCKET=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/s3_bucket" --query Parameter.Value --output text)
ADMIN_PW=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/admin_password" --with-decryption --query Parameter.Value --output text)
JAR=$(mktemp)
# Log in as the bootstrap admin and trigger an in-process EXPORT DATABASE.
curl -fsS -c "$JAR" -X POST http://127.0.0.1:8741/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"admin@local\",\"password\":\"$ADMIN_PW\"}" >/dev/null
curl -fsS -b "$JAR" -X POST http://127.0.0.1:8741/api/admin/backup >/dev/null
rm -f "$JAR"
# Ship the archives off-box.
aws s3 sync "$MOUNT/backups/" "s3://$S3_BUCKET/backups/" --no-progress
BACKUP
chmod +x /usr/local/bin/elenchus-backup.sh

cat > /etc/systemd/system/elenchus-backup.service <<'BSVC'
[Unit]
Description=Elenchus PoC backup → S3
[Service]
Type=oneshot
ExecStart=/usr/local/bin/elenchus-backup.sh
BSVC
cat > /etc/systemd/system/elenchus-backup.timer <<'BTMR'
[Unit]
Description=Daily Elenchus PoC backup
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
BTMR
systemctl daemon-reload
systemctl enable --now elenchus-backup.timer

echo "elenchus PoC bootstrap complete: https://$HOSTNAME_FQDN/"
