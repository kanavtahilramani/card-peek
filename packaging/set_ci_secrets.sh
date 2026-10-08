#!/bin/bash
# Give GitHub Actions what it needs to sign and notarize releases. Run it yourself: it
# uploads your signing key to this repository's Actions secrets.
#
#   packaging/set_ci_secrets.sh DeveloperID.p12 AuthKey_ABC123.p8 <key id> <issuer id>
#
# DeveloperID.p12: in Keychain Access > My Certificates, right-click "Developer ID
#   Application: …" > Export, and set a password (you'll be asked for it here).
# AuthKey_….p8, key id, issuer id: App Store Connect > Users and Access > Integrations >
#   App Store Connect API > Team Keys, a key with Developer access.
set -euo pipefail
[ $# -eq 4 ] || { sed -n '2,12p' "$0"; exit 1; }
P12="$1" P8="$2" KEY_ID="$3" ISSUER="$4"

read -rsp "Password of $P12: " P12_PASSWORD; echo
openssl pkcs12 -in "$P12" -passin "pass:$P12_PASSWORD" -nokeys -legacy 2>/dev/null | grep -q "Developer ID Application" \
    || openssl pkcs12 -in "$P12" -passin "pass:$P12_PASSWORD" -nokeys 2>/dev/null | grep -q "Developer ID Application" \
    || { echo "$P12 doesn't hold a Developer ID Application certificate (or the password is wrong)."; exit 1; }

base64 -i "$P12" | gh secret set MACOS_CERT_P12
printf %s "$P12_PASSWORD" | gh secret set MACOS_CERT_PASSWORD
base64 -i "$P8" | gh secret set NOTARY_KEY_P8
printf %s "$KEY_ID" | gh secret set NOTARY_KEY_ID
printf %s "$ISSUER" | gh secret set NOTARY_ISSUER
gh secret list
