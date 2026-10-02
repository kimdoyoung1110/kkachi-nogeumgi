#!/bin/bash
# 까치녹음기 설치 (두 번 눌러 실행). 다시 실행하면 최신 버전으로 업데이트돼요.
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ok() { printf "\033[32m  ✓ %s\033[0m\n" "$1"; }
fail() {
  printf "\n\033[31m  ✗ %s\033[0m\n\n" "$1"
  read -r -n 1 -p "아무 키나 누르면 창이 닫혀요." || true
  exit 1
}

clear
cat <<'BANNER'

   🐦‍⬛  까치녹음기 설치

   인터넷이 연결된 상태에서 진행해 주세요.
   처음 설치는 모델을 받느라 10~30분쯤 걸려요.

BANNER

bold "0/5 이 맥이 쓸 수 있는지 확인"
[ "$(uname -m)" = "arm64" ] || fail "애플 실리콘(M1 이상) 맥에서만 쓸 수 있어요."
FREE_GB=$(df -g "$HOME" | awk 'NR==2 {print $4}')
[ "${FREE_GB:-0}" -ge 8 ] || fail "저장 공간이 8GB 이상 필요해요. (지금 ${FREE_GB}GB 남음)"
ok "애플 실리콘 맥 · 남은 공간 ${FREE_GB}GB"

bold "1/5 설치 도구(uv) 준비"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv 를 설치하지 못했어요. 인터넷 연결을 확인해 주세요."
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "$(uv --version)"

bold "2/5 Python과 라이브러리 설치"
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  git pull --ff-only --quiet 2>/dev/null && ok "최신 코드 받음" || echo "  (최신 코드를 받지 못해서 지금 있는 버전으로 설치해요)"
fi
uv sync --frozen --no-dev || fail "라이브러리를 설치하지 못했어요. 인터넷 연결을 확인해 주세요."
ok "라이브러리 설치 완료"

bold "3/5 받아쓰기 모델 내려받기 (약 4GB)"
.venv/bin/python -m app.bootstrap download || fail "모델을 받지 못했어요. 인터넷 연결을 확인하고 다시 실행해 주세요."

bold "4/5 시험 받아쓰기"
PYTHONWARNINGS=ignore .venv/bin/python -m app.bootstrap selftest || fail "시험 받아쓰기가 실패했어요. 이 창 내용을 사진 찍어서 보내주세요."

bold "5/5 까치녹음기 앱 만들기"
# 이미 켜져 있으면 끄고 새로 만든다 (업데이트 반영)
curl -fsS -X POST --max-time 3 http://127.0.0.1:8765/api/app/quit >/dev/null 2>&1 && sleep 2
# KKACHI_APP_DIR: 앱을 둘 폴더 (기본은 응용 프로그램 폴더, 리허설할 때만 바꿈)
APP="$(bash scripts/build_app.sh "$REPO" "${KKACHI_APP_DIR:-}")" || fail "앱을 만들지 못했어요."
ok "$APP"

cat <<'DONE'

   ✅ 설치가 끝났어요!

   · 응용 프로그램 폴더의 '까치녹음기'를 Dock 으로 끌어다 두면 편해요.
   · 처음 녹음할 때 브라우저가 마이크 권한을 물어보면 '허용'을 눌러주세요.
   · 앞으로 업데이트는 앱 안의 ⚙ › 업데이트 확인 으로 하면 돼요.

DONE
open -R "$APP"
open "$APP"
read -r -n 1 -p "아무 키나 누르면 이 창이 닫혀요." || true
exit 0
