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
cp "$REPO/assets/kkachi-launcher" "$APP/Contents/MacOS/kkachi"
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
  <key>CFBundleShortVersionString</key><string>1.0.3</string>
  <key>CFBundleVersion</key><string>4</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>강의와 회의를 녹음하려면 마이크가 필요해요.</string>
</dict>
</plist>
PLIST

# 앱 전체를 (임시) 서명한다. 실행 파일만 서명돼 있으면 macOS 가 앱으로 인정하지 않아
# 알림 권한을 줄 수 없다 (알림 설정 목록에도 안 나타남 — 실제로 겪음)
# 임시 서명은 기본으로 '실행 파일 지문(cdhash)'으로 앱을 알아봐서, 업데이트로 앱을 새로 만들 때마다
# 전체 디스크 접근·화면 녹음 권한이 켜진 채로 보이는데도 안 먹었다 (실제로 겪음).
# → '앱 이름표(identifier)'로 알아보게 해서 권한이 업데이트 뒤에도 이어지게 한다.
codesign --force --sign - --identifier com.kkachi.nogeumgi \
  -r='designated => identifier "com.kkachi.nogeumgi"' "$APP" >/dev/null 2>&1 \
  || codesign --force --sign - --identifier com.kkachi.nogeumgi "$APP" >/dev/null 2>&1 || true

touch "$APP"  # Finder 가 아이콘을 새로 읽게
# 업데이트 때 앱을 다시 만들 수 있도록 위치를 적어둔다
DATA="$HOME/Library/Application Support/KkachiNogeumgi"
mkdir -p "$DATA" && printf '%s' "$APP" > "$DATA/app_path"
echo "$APP"
