#!/usr/bin/env bash
set -euo pipefail

APP_NAME="wzc"
DOMAIN=""
SSH_HOST=""
APP_PORT="3000"
SSH_PORT="22"

usage() {
  cat <<USAGE
Usage: $0 --host user@server [--domain example.com] [--app-name wzc] [--app-port 3000] [--ssh-port 22]

Options:
  --host       SSH target for the VPS, for example root@203.0.113.10. Required.
  --domain     Domain name pointing to the VPS. Optional.
  --app-name   Application name used for paths and service names. Default: wzc.
  --app-port   Local application port nginx should proxy to. Default: 3000.
  --ssh-port   SSH port for connecting to the VPS. Default: 22.
  -h, --help   Show this help text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      SSH_HOST="${2:-}"
      shift 2
      ;;
    --domain)
      DOMAIN="${2:-}"
      shift 2
      ;;
    --app-name)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --app-port)
      APP_PORT="${2:-}"
      shift 2
      ;;
    --ssh-port)
      SSH_PORT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$SSH_HOST" ]]; then
  echo "Missing required --host argument." >&2
  usage >&2
  exit 1
fi

SERVER_NAME="_"
if [[ -n "$DOMAIN" ]]; then
  SERVER_NAME="$DOMAIN"
fi

ssh -p "$SSH_PORT" "$SSH_HOST" \
  "APP_NAME='$APP_NAME' DOMAIN='$DOMAIN' APP_PORT='$APP_PORT' SERVER_NAME='$SERVER_NAME' bash -s" <<'REMOTE'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl git nginx ufw certbot python3-certbot-nginx

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

install -d -m 0755 "/opt/${APP_NAME}/current"

cat > "/etc/nginx/sites-available/${APP_NAME}" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_NAME};

    access_log /var/log/nginx/${APP_NAME}.access.log;
    error_log /var/log/nginx/${APP_NAME}.error.log;

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}
NGINX

ln -sfn "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [[ -n "$DOMAIN" ]]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || {
    echo "Certbot failed. Check DNS records and rerun certbot manually." >&2
  }
fi

echo "VPS initialization complete. Application directory: /opt/${APP_NAME}/current"
REMOTE
