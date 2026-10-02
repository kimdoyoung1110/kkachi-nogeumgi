#!/bin/bash
# 까치녹음기.app 실행기. 서버가 꺼져 있으면 켜고, 브라우저로 화면을 연다.
# (build_app.sh 가 이 파일을 .app 안에 넣는다. 설치 폴더 위치는 Resources/repo_path 에 적혀 있다)

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cat "$HERE/../Resources/repo_path" 2>/dev/null)"
PORT="${KKACHI_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
DATA="$HOME/Library/Application Support/KkachiNogeumgi"
LOGDIR="$DATA/logs"
mkdir -p "$LOGDIR"

alert() {
  osascript -e "display alert \"$1\" message \"$2\" as critical" >/dev/null 2>&1
}

is_ours() {
  curl -fsS --max-time 2 "$URL/api/health" 2>/dev/null | grep -q '"app":"kkachi"'
}

# 이미 켜져 있으면 화면만 연다
if is_ours; then
  open "$URL"
  exit 0
fi

# 다른 프로그램이 같은 번호를 쓰고 있는 경우
if curl -fsS --max-time 2 "$URL" >/dev/null 2>&1; then
  alert "까치녹음기를 켤 수 없어요" "다른 프로그램이 $PORT 번 통로를 쓰고 있어요. 맥을 재시동한 뒤 다시 눌러주세요."
  exit 1
fi

PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  alert "까치녹음기 설치를 찾을 수 없어요" "설치 폴더가 옮겨졌거나 지워졌어요. 설치를 다시 해주세요. ($REPO)"
  exit 1
fi

cd "$REPO" || exit 1
echo "---- $(date '+%Y-%m-%d %H:%M:%S') 앱 시작 ----" >> "$LOGDIR/server.log"
# HF_HUB_OFFLINE: 실행 중에는 인터넷에 접속하지 않는다 (모델은 설치할 때 받아둠)
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYTHONWARNINGS=ignore \
  nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >> "$LOGDIR/server.log" 2>&1 &
disown

for _ in $(seq 1 120); do
  if is_ours; then
    open "$URL"
    exit 0
  fi
  sleep 0.5
done

alert "까치녹음기를 켜지 못했어요" "잠시 후 다시 눌러보고, 계속되면 바탕화면의 로그를 보내주세요."
cp "$LOGDIR/server.log" "$HOME/Desktop/까치녹음기-시작실패-로그.txt" 2>/dev/null
exit 1
