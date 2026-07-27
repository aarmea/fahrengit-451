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
# Run it BEFORE `docker compose up -d fdroid`: the service bind-mounts
# config/fdroid-keystore.jks, and Docker silently creates a *directory* at that
# path if the file does not exist yet.
#
# Prerequisites:
#   • keytool on the host — a JRE is enough, it does not need a full JDK:
#       sudo apt install --no-install-recommends default-jre-headless
#     (the JDK requirement is inside the fdroid image, where fdroidserver signs
#     the index with jarsigner; see fdroid/Dockerfile)
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

DOMAIN="${DOMAIN:?DOMAIN must be set in .env}"

# The keystore password is not a secret you have to supply from somewhere — it
# is an arbitrary value guarding a file that lives next to it. So generate one
# rather than making you invent it, and write it back to .env.
#
# One password, not two: keytool's default keystore format is PKCS12, which
# does not support a key password distinct from the store password — it prints
# "Ignoring user-specified -keypass value" and uses the store password for
# both. Two different values in .env would therefore produce a key that
# fdroidserver cannot use, with a confusing error much later.
GENERATED="false"
STOREPASS="${FDROID_KEYSTOREPASS:-}"

if [[ -z "$STOREPASS" ]]; then
    STOREPASS="$(openssl rand -hex 24 2>/dev/null \
        || tr -dc 'a-f0-9' < /dev/urandom | head -c 48)"
    GENERATED="true"
elif [[ -n "${FDROID_KEYPASS:-}" && "${FDROID_KEYPASS}" != "$STOREPASS" ]]; then
    echo "ERROR: FDROID_KEYPASS differs from FDROID_KEYSTOREPASS."
    echo "       keytool's PKCS12 keystores do not support separate key and store"
    echo "       passwords — set both to the same value, or blank them out and let"
    echo "       this script generate one."
    exit 1
fi

if [[ -f "$KEYSTORE" ]]; then
    echo "ERROR: $KEYSTORE already exists."
    echo "       Refusing to overwrite it — replacing this key would orphan every"
    echo "       device that has already added the repository."
    exit 1
fi

if ! command -v keytool >/dev/null 2>&1; then
    echo "ERROR: keytool not found. A JRE is enough:"
    echo "       sudo apt install --no-install-recommends default-jre-headless"
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
    -keypass "$STOREPASS" \
    -dname "CN=${DOMAIN} F-Droid repo"

chmod 0600 "$KEYSTORE"

if [[ "$GENERATED" == "true" ]]; then
    echo "==> Writing the generated password into .env..."
    # Rewrite through a temp file, then copy back, so .env keeps its inode and
    # permissions. Both variables get the same value — see the note above.
    tmp="$(mktemp)"
    grep -vE '^(FDROID_KEYSTOREPASS|FDROID_KEYPASS)=' .env > "$tmp" || true
    printf 'FDROID_KEYSTOREPASS=%s\nFDROID_KEYPASS=%s\n' "$STOREPASS" "$STOREPASS" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
fi

# keytool prints "Certificate fingerprint (SHA-256): AB:CD:…" — note the hyphen,
# and the colons. F-Droid wants it as lowercase hex with neither.
FINGERPRINT="$(keytool -list -keystore "$KEYSTORE" -alias "$ALIAS" \
    -storepass "$STOREPASS" 2>/dev/null \
    | awk -F': ' '/SHA-?256/ { gsub(/:/, "", $2); print tolower($2); exit }')"

if [[ -z "$FINGERPRINT" ]]; then
    # Not fatal: the keystore exists and is usable, we just could not read the
    # fingerprint back for the message below.
    FINGERPRINT="<run: keytool -list -keystore $KEYSTORE -alias $ALIAS>"
fi

cat <<EOF

==> Done. $KEYSTORE created (mode 0600), password stored in .env.

    Add the repository on a device with:

        https://${DOMAIN}/fdroid/repo?fingerprint=${FINGERPRINT}

    or open https://${DOMAIN}/fdroid/repo/ and scan the QR code there once the
    service has published for the first time.

    BACK UP $KEYSTORE somewhere off this machine. If it is lost, every device
    has to remove and re-add the repository under a new fingerprint.
EOF
