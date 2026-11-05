#!/bin/bash
# Build Deceive.app (universal2, ad-hoc signed) and optionally a DMG.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${VERSION:-1.0.0}"
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  x86_64|i386) LABEL="x64" ;;
  *) LABEL="$ARCH" ;;
esac
BUNDLE_ID="com.deceivemac.deceive"
APP="dist/Deceive.app"

python3 scripts/make_icon.py

rm -rf build dist
python3 -m PyInstaller \
  --noconfirm --clean --windowed --onedir \
  --name Deceive \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --icon "$PWD/assets/AppIcon.icns" \
  --target-arch "$ARCH" \
  --paths "$PWD" \
  --specpath build \
  --distpath dist \
  --workpath build \
  scripts/entry.py

PLIST="$APP/Contents/Info.plist"
set_bool() { /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PLIST" 2>/dev/null || \
             /usr/libexec/PlistBuddy -c "Add :$1 bool $2" "$PLIST"; }
set_string() { /usr/libexec/PlistBuddy -c "Set :$1 $2" "$PLIST" 2>/dev/null || \
               /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST"; }

set_bool LSUIElement true
set_bool NSHighResolutionCapable true
set_string CFBundleShortVersionString "$VERSION"
set_string CFBundleVersion "$VERSION"
set_string NSHumanReadableCopyright "Deceive is licensed under the GNU GPL v3."

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "built $APP ($(/usr/bin/lipo -archs "$APP/Contents/MacOS/Deceive"))"

if [ "${MAKE_DMG:-1}" = "1" ]; then
  rm -rf dist/dmg && mkdir -p dist/dmg
  cp -R "$APP" dist/dmg/
  ln -s /Applications dist/dmg/Applications
  hdiutil create -volname "Deceive" -srcfolder dist/dmg -ov -format UDZO \
    "dist/Deceive-$VERSION-$LABEL.dmg" >/dev/null
  echo "built dist/Deceive-$VERSION-$LABEL.dmg"
fi
