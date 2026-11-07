#!/usr/bin/env bash
# Builds ScanStack.app and a .dmg for Apple silicon Macs.
# GitHub Actions runs this (see .github/workflows/build-macos.yml), but it also
# works on any Mac with Python 3.12 and the packages in requirements-build.txt.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"

VERSION="${VERSION:-$(tr -d '[:space:]' < VERSION)}"
BUILD_NUMBER="${BUILD_NUMBER:-1}"
APP="dist/ScanStack.app"
DMG="dist/ScanStack-${VERSION}-macOS-arm64.dmg"

step() { printf '\n==> %s\n' "$*"; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script builds the Mac app and has to run on macOS." >&2
  exit 1
fi

rm -rf build dist
mkdir -p build

# ---------------------------------------------------------------- 1. Icon
step "Making the app icon"
python packaging/macos/make_iconset.py scanstack.svg build/ScanStack.iconset
iconutil -c icns build/ScanStack.iconset -o build/ScanStack.icns

# ---------------------------------------------------------------- 2. App bundle
step "Packaging ScanStack.app with PyInstaller"
printf '%s\n' "$VERSION" > build/VERSION
pyinstaller --noconfirm --clean --log-level WARN \
  --windowed --name ScanStack \
  --osx-bundle-identifier com.jrhfilms.scanstack \
  --target-arch arm64 \
  --icon "$ROOT/build/ScanStack.icns" \
  --add-data "$ROOT/scanstack.svg:." \
  --add-data "$ROOT/build/VERSION:." \
  --hidden-import pillow_heif --collect-binaries pillow_heif \
  --exclude-module tkinter --exclude-module numpy \
  --specpath build --workpath build/pyinstaller --distpath dist \
  main.py

# ---------------------------------------------------------------- 3. Info.plist
step "Setting version and file types in Info.plist"
PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "$PLIST"
plutil -replace NSHumanReadableCopyright -string "JRH Films" "$PLIST"
plutil -replace LSMinimumSystemVersion -string "14.0" "$PLIST"
plutil -replace NSHighResolutionCapable -bool true "$PLIST"
# Lets you drop scans and folders on the Dock icon, and use Finder's "Open With".
plutil -replace CFBundleDocumentTypes -json '[
  {"CFBundleTypeName":"Image","CFBundleTypeRole":"Viewer","LSHandlerRank":"Alternate",
   "LSItemContentTypes":["public.image"]},
  {"CFBundleTypeName":"Folder","CFBundleTypeRole":"Viewer","LSHandlerRank":"Alternate",
   "LSItemContentTypes":["public.folder"]}
]' "$PLIST"
plutil -lint "$PLIST"

step "Signing (ad-hoc, so Apple silicon will run it)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

# ---------------------------------------------------------------- 4. Test the packaged app
step "Checking the packaged app can make PDFs in every mode"
"$APP/Contents/MacOS/ScanStack" --self-test

# ---------------------------------------------------------------- 5. Disk image
step "Making $DMG"
mkdir -p build/dmg
cp -R "$APP" build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -quiet -volname "ScanStack" -srcfolder build/dmg -ov -format UDZO "$DMG"
( cd dist && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )
cat "$DMG.sha256"
du -h "$DMG"

step "Done"
