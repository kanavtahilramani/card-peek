#!/bin/bash
# Sign Card Peek.app for distribution: every binary inside it, deepest first, then the
# app, all with the hardened runtime and a secure timestamp (both needed to notarize).
#
#   packaging/sign_mac.sh "dist/Card Peek.app" "Developer ID Application: Name (TEAMID)"
#
# The identity can also come from $CODESIGN_IDENTITY. "-" signs ad hoc (local testing).
set -euo pipefail

APP="$1"
IDENTITY="${2:-${CODESIGN_IDENTITY:?pass a signing identity or set CODESIGN_IDENTITY}}"
ENTITLEMENTS="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"
TIMESTAMP=--timestamp
[ "$IDENTITY" = "-" ] && TIMESTAMP=--timestamp=none

sign() {
    codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP" "$@"
}

# Loose binaries (Python extensions, dylibs), longest paths first so nested code is
# signed before whatever contains it. The main executable is signed with the app.
find "$APP/Contents" -type f ! -path "*/Contents/MacOS/*" -print0 \
    | xargs -0 file --no-pad --separator '|' \
    | awk -F'|' '/Mach-O/ { print length($1) "\t" $1 }' \
    | sort -rn | cut -f2- \
    | while IFS= read -r f; do sign "$f"; done

# Embedded frameworks are bundles and get sealed as one.
find "$APP/Contents/Frameworks" -maxdepth 3 -type d -path "*.framework/Versions/*" ! -name Current \
    | while IFS= read -r fw; do sign "$fw"; done

sign --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
