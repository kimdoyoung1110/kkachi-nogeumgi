"""선물 설정: 여자친구 이름과 편지를 이 맥에만 저장한다 (저장소에는 올라가지 않음).

    .venv/bin/python -m app.gift          # 물어보는 대로 입력
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

    print("화면 인사에 쓸 이름이나 애칭 (예: 지은):")
    name = input("> ").strip()
    print("편지에 적을 보내는 사람 이름 (예: 도영):")
    sender = input("> ").strip()
    print("편지 내용. 여러 줄로 써도 돼요. 다 쓰면 빈 줄에서 Enter 두 번:")
    lines, blank = [], 0
    while True:
        line = input()
        if not line.strip():
            blank += 1
            if blank >= 2 or (blank and not lines):
                break
            lines.append("")
            continue
        blank = 0
        lines.append(line)
    letter = "\n".join(lines).strip()

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "from": sender, "letter": letter}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"저장했어요: {path}")
    print("다음에 까치녹음기를 켜면 처음 한 번 편지가 떠요. (⚙ › 편지 다시 보기 로도 볼 수 있어요)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
