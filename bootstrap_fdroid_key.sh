#!/usr/bin/env bash
# bootstrap_fdroid_key.sh
# ─────────────────────────────────────────────────────────────────────────────
# Run this ONCE before starting the optional `fdroid` service, to create the key
# that signs the F-Droid repository index.
#
# THIS KEY IS PERMANENT. Its SHA-256 fingerprint is embedded in the repository
# URL that every device is configured with, so losing or rotating it means every
# device has to remove and re-add the repository. It is the only piece of state
# in this stack that cannot be regenerated — back it up off this machine.
#
# Prerequisites:
#   • a JDK on the host (for keytool), or run it inside the fdroid image:
#       docker compose run --rm --entrypoint bash fdroid /app/bootstrap.sh
#   • .env present, with FDROID_KEYSTOREPASS / FDROID_KEYPASS filled in
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

KEYSTORE="./config/fdroid-keystore.jks"
ALIAS="fdroid-index"

if [[ ! -f .env ]]; then
    echo "ERROR: .env file not found. Copy .env.example → .env and fill in your values."
    exit 1
fi

# shellcheck disable=SC1091
source .env

STOREPASS="${FDROID_KEYSTOREPASS:?FDROID_KEYSTOREPASS must be set in .env}"
KEYPASS="${FDROID_KEYPASS:?FDROID_KEYPASS must be set in .env}"
DOMAIN="${DOMAIN:?DOMAIN must be set in .env}"

if [[ -f "$KEYSTORE" ]]; then
    echo "ERROR: $KEYSTORE already exists."
    echo "       Refusing to overwrite it — replacing this key would orphan every"
    echo "       device that has already added the repository."
    exit 1
fi

if ! command -v keytool >/dev/null 2>&1; then
    echo "ERROR: keytool not found. Install a JDK (e.g. default-jdk-headless),"
    echo "       or run this inside the fdroid service image."
    exit 1
fi

echo "==> Creating the F-Droid index signing key..."
keytool -genkeypair -v \
    -keystore "$KEYSTORE" \
    -alias "$ALIAS" \
    -keyalg RSA \
    -keysize 4096 \
    -validity 10000 \
    -storepass "$STOREPASS" \
    -keypass "$KEYPASS" \
    -dname "CN=${DOMAIN} F-Droid repo"

chmod 0600 "$KEYSTORE"

FINGERPRINT="$(keytool -list -keystore "$KEYSTORE" -alias "$ALIAS" \
    -storepass "$STOREPASS" | grep SHA256 | tr -d ': ' | tail -c 65)"

cat <<EOF

==> Done. $KEYSTORE created (mode 0600).

    Add the repository on a device with:

        https://${DOMAIN}/fdroid/repo?fingerprint=${FINGERPRINT}

    or open https://${DOMAIN}/fdroid/repo/ and scan the QR code there once the
    service has published for the first time.

    BACK UP $KEYSTORE somewhere off this machine. If it is lost, every device
    has to remove and re-add the repository under a new fingerprint.
EOF
