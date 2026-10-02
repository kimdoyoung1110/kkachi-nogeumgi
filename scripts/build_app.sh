#!/bin/bash
# 까치녹음기.app 을 만든다.  사용법: build_app.sh <설치 폴더> [앱을 둘 폴더]
set -euo pipefail
REPO="$(cd "$1" && pwd)"
DEST="${2:-}"
if [ -z "$DEST" ]; then
  if [ -w /Applications ]; then DEST="/Applications"; else DEST="$HOME/Applications"; fi
fi
mkdir -p "$DEST"
APP="$DEST/까치녹음기.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/scripts/launcher.sh" "$APP/Contents/MacOS/kkachi"
chmod +x "$APP/Contents/MacOS/kkachi"
cp "$REPO/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
printf '%s' "$REPO" > "$APP/Contents/Resources/repo_path"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>까치녹음기</string>
  <key>CFBundleDisplayName</key><string>까치녹음기</string>
  <key>CFBundleIdentifier</key><string>com.kkachi.nogeumgi</string>
  <key>CFBundleExecutable</key><string>kkachi</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

touch "$APP"  # Finder 가 아이콘을 새로 읽게
echo "$APP"
