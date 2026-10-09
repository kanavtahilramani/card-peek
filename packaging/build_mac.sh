#!/bin/bash
# Build Card Peek.app and a DMG for it, signed and (when credentials are given) notarized.
#
#   packaging/build_mac.sh
#
# Environment:
#   CODESIGN_IDENTITY   "Developer ID Application: …" to sign for release; "-" (the
#                       default) signs ad hoc, which only runs on this Mac.
#   NOTARY_PROFILE      a notarytool keychain profile (xcrun notarytool store-credentials), or
#   NOTARY_KEY, NOTARY_KEY_ID, NOTARY_ISSUER
#                       an App Store Connect API key (.p8 path, key ID, issuer ID).
#                       With neither, the DMG isn't notarized.
#
# Output: dist/Card Peek.app and dist/CardPeek-<version>.dmg
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
IDENTITY="${CODESIGN_IDENTITY:--}"
VERSION="$("$PYTHON" -c 'import cardpeek; print(cardpeek.__version__)')"
APP="dist/Card Peek.app"
DMG="dist/CardPeek-$VERSION.dmg"

echo "==> Building Card Peek $VERSION"
rm -rf build/CardPeek "$APP" "$DMG"
"$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build packaging/CardPeek-mac.spec

echo "==> Signing as: $IDENTITY"
packaging/sign_mac.sh "$APP" "$IDENTITY" > build/sign.log 2>&1 || { cat build/sign.log; exit 1; }
tail -2 build/sign.log

echo "==> Self-test of the built app"
"$APP/Contents/MacOS/Card Peek" --self-test

echo "==> Making $DMG"
# dmgbuild lays out the window (packaging/dmg_settings.py) without needing Finder.
# hdiutil, which it runs, now and then fails on a busy CI machine ("Resource busy"), so give
# it a few goes.
for attempt in 1 2 3; do
    if "$PYTHON" -m dmgbuild -s packaging/dmg_settings.py -D app="$APP" "Card Peek" "$DMG" > build/dmg.log 2>&1; then
        break
    fi
    cat build/dmg.log
    [ "$attempt" -lt 3 ] || exit 1
    echo "==> hdiutil failed; trying again"
    sleep 15
done
if [ "$IDENTITY" != "-" ]; then
    codesign --force --sign "$IDENTITY" --timestamp "$DMG"
fi

notary=()
if [ -n "${NOTARY_PROFILE:-}" ]; then
    notary=(--keychain-profile "$NOTARY_PROFILE")
elif [ -n "${NOTARY_KEY:-}" ]; then
    notary=(--key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER")
fi
if [ ${#notary[@]} -gt 0 ] && [ "$IDENTITY" != "-" ]; then
    echo "==> Notarizing (usually a few minutes)"
    out="$(xcrun notarytool submit "$DMG" "${notary[@]}" --wait --output-format json)"
    echo "$out"
    if ! grep -q '"status" *: *"Accepted"' <<<"$out"; then
        id="$(sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' <<<"$out" | head -1)"
        [ -n "$id" ] && xcrun notarytool log "$id" "${notary[@]}" || true
        exit 1
    fi
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
    # Check what Gatekeeper will make of the app inside, as a downloaded copy would be.
    mnt="$(mktemp -d)"
    hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mnt" "$DMG"
    spctl --assess --type execute --verbose=2 "$mnt/Card Peek.app"
    hdiutil detach -quiet "$mnt"
else
    echo "==> Not notarized (no credentials, or ad-hoc signed)"
fi

ls -lh "$DMG" | awk '{print "==> " $9 ": " $5}'
