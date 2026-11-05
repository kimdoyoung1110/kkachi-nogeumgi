"""이 맥의 Claude 앱에 까치 영상분석기를 연결한다 (영상 탭의 'Claude 앱에 연결' 버튼).

Claude 앱 설정 파일의 mcpServers 에 항목 하나만 넣는다. 나머지 설정은 그대로 두고, 처음 고칠 때 복사본을 남긴다.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

SERVER_NAME = "kkachi-video"
CONFIG_PATH = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"


def server_entry(uv: str, repo: Path) -> dict[str, Any]:
    # --no-sync: 라이브러리는 앱 설치·업데이트 때 이미 맞춰 두었다
    return {"command": uv, "args": ["run", "-q", "--no-sync", "--directory", str(repo), "python", "-m", "app.video_mcp"],
            "env": {"PYTHONWARNINGS": "ignore"}}


def status(uv: str, repo: Path, path: Path = CONFIG_PATH) -> str:
    """no_claude: Claude 앱이 없음 / off: 연결 안 됨 / on: 연결됨"""
    if not path.parent.exists():
        return "no_claude"
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError):
        return "off"
    return "on" if servers.get(SERVER_NAME) == server_entry(uv, repo) else "off"


def connect(uv: str, repo: Path, path: Path = CONFIG_PATH) -> None:
    if not path.parent.exists():
        raise ValueError("이 맥에서 Claude 앱을 찾지 못했어요. Claude 앱을 설치하고 한 번 켠 뒤 다시 눌러 주세요.")
    config: dict[str, Any] = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("Claude 앱 설정 파일을 읽지 못해서 고치지 않았어요.") from exc
        backup = path.with_name(path.name + ".before-kkachi")
        if not backup.exists():
            shutil.copy2(path, backup)
    config.setdefault("mcpServers", {})[SERVER_NAME] = server_entry(uv, repo)
    tmp = path.with_name(path.name + ".kkachi-tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
