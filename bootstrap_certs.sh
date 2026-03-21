#!/usr/bin/env bash
# bootstrap_certs.sh
# ─────────────────────────────────────────────────────────────────────────────
# Run this ONCE before `docker compose up -d` to obtain the initial Let's
# Encrypt certificate.  nginx must be able to serve the ACME challenge, so we
# bring up only the services needed for that, run certbot, then download the
# Certbot recommended TLS options, and finally start everything.
#
# Prerequisites:
#   • docker compose v2 installed
#   • DNS for $DOMAIN already pointing to this server's IP
#   • Ports 80 and 443 open in your firewall
#   • .env file present (copy from .env.example and fill in)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

if [[ ! -f .env ]]; then
    echo "ERROR: .env file not found. Copy .env.example → .env and fill in your values."
    exit 1
fi

# shellcheck disable=SC1091
source .env

DOMAIN="${DOMAIN:?DOMAIN must be set in .env}"
EMAIL="${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL must be set in .env}"
CERTS_DIR="./certs"

echo "==> Creating certificate directory structure..."
mkdir -p "${CERTS_DIR}/live/${DOMAIN}"
mkdir -p "${CERTS_DIR}/archive"

# ── Download Certbot recommended TLS options ──────────────────────────────────
if [[ ! -f "${CERTS_DIR}/options-ssl-nginx.conf" ]]; then
    echo "==> Downloading recommended TLS options..."
    curl -sSL \
        "https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf" \
        -o "${CERTS_DIR}/options-ssl-nginx.conf"
fi

if [[ ! -f "${CERTS_DIR}/ssl-dhparams.pem" ]]; then
    echo "==> Downloading DH parameters..."
    curl -sSL \
        "https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem" \
        -o "${CERTS_DIR}/ssl-dhparams.pem"
fi

# ── Create a dummy certificate so nginx can start (needed for ACME challenge) ─
DUMMY_LIVE="${CERTS_DIR}/live/${DOMAIN}"
if [[ ! -f "${DUMMY_LIVE}/fullchain.pem" ]]; then
    echo "==> Generating temporary self-signed certificate..."
    openssl req -x509 -nodes -newkey rsa:4096 -days 1 \
        -keyout "${DUMMY_LIVE}/privkey.pem" \
        -out    "${DUMMY_LIVE}/fullchain.pem" \
        -subj   "/CN=${DOMAIN}"
fi

# ── Start nginx (and dependencies) ───────────────────────────────────────────
echo "==> Starting nginx with temporary certificate..."
docker compose up -d nginx forgejo geoipupdate geoblock_watcher

echo "==> Waiting for nginx to be ready..."
sleep 5

# ── Obtain the real certificate via webroot challenge ────────────────────────
echo "==> Requesting Let's Encrypt certificate for ${DOMAIN}..."
docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot \
    --webroot-path /var/www/certbot \
    --email "${EMAIL}" \
    --agree-tos \
    --no-eff-email \
    -d "${DOMAIN}"

# ── Reload nginx with the real certificate ────────────────────────────────────
echo "==> Reloading nginx with the real certificate..."
docker compose exec nginx nginx -s reload

# ── Start remaining services ──────────────────────────────────────────────────
echo "==> Starting all services..."
docker compose up -d

echo ""
echo "✓ Bootstrap complete.  Your Git service should be live at https://${DOMAIN}/"
echo ""
echo "Next steps:"
echo "  1. Visit https://${DOMAIN}/ and complete the Forgejo setup wizard."
echo "  2. Create your admin account."
echo "  3. Set DISABLE_REGISTRATION=true in .env, then: docker compose up -d forgejo"
echo "  4. Edit geo_rules.yml to configure per-repo geo-blocking."
