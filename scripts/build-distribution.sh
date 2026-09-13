#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/app"
DIST="$ROOT/dist"
VERSION="$(node -p "require('$APP_DIR/package.json').version")"
APP="$DIST/mac-arm64/Phantom.app"
DMG="$DIST/Phantom-${VERSION}-arm64.dmg"
CHECKSUM="$DMG.sha256"
STAGE="$(mktemp -d /tmp/phantom-release.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

cd "$APP_DIR"
npm run build:runtime
npx electron-builder --mac dir --arm64

codesign --force --deep --sign - --identifier com.ctwebsolutions.phantom "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"

mkdir -p "$STAGE/Phantom ${VERSION}"
ditto "$APP" "$STAGE/Phantom ${VERSION}/Phantom.app"
ln -s /Applications "$STAGE/Phantom ${VERSION}/Applications"
cp "$ROOT/DISTRIBUTION_README.txt" "$STAGE/Phantom ${VERSION}/Read Me.txt"

rm -f "$DMG" "$CHECKSUM"
hdiutil create \
  -volname "Phantom ${VERSION}" \
  -srcfolder "$STAGE/Phantom ${VERSION}" \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG"

shasum -a 256 "$DMG" > "$CHECKSUM"
hdiutil verify "$DMG"

echo "Release: $DMG"
cat "$CHECKSUM"
