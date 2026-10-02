"""선물 설정: 여자친구 이름(애칭)을 이 맥에만 저장한다 (저장소에는 올라가지 않음).

화면 이름이 "○○의 까치녹음기"가 되고, 시간대별 인사와 받아쓰기 끝남 알림에서 이름을 불러준다.

    .venv/bin/python -m app.gift          # 이름 입력
    .venv/bin/python -m app.gift --show   # 지금 저장된 내용 보기
    .venv/bin/python -m app.gift --clear  # 지우기

저장 위치: ~/Library/Application Support/KkachiNogeumgi/gift.json
"""

from __future__ import annotations

import json
import sys

from app import config


def main() -> int:
    path = config.DATA_DIR / "gift.json"
    if "--show" in sys.argv:
        print(path.read_text(encoding="utf-8") if path.exists() else "(아직 없어요)")
        return 0
    if "--clear" in sys.argv:
        path.unlink(missing_ok=True)
        print("지웠어요.")
        return 0

    print("화면 인사와 알림에 쓸 이름이나 애칭 (예: 지은):")
    name = input("> ").strip()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장했어요: {path}")
    print("까치녹음기를 다시 켜면 적용돼요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
