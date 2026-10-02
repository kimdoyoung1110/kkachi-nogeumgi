#!/bin/bash
# 개발자용: Swift 실행기를 컴파일해서 assets/kkachi-launcher 로 저장한다 (결과물은 저장소에 커밋).
set -euo pipefail
cd "$(dirname "$0")/.."
swiftc -O -target arm64-apple-macos13 -framework AppKit scripts/launcher.swift -o assets/kkachi-launcher
codesign --force --sign - assets/kkachi-launcher
echo "assets/kkachi-launcher ($(du -h assets/kkachi-launcher | cut -f1))"
