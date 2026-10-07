"""앱 자체 관리: 로그, 버전, 업데이트, 문제 신고용 로그 묶음, 종료/재시작."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO_DIR = Path(__file__).resolve().parent.parent
BRANCH = "main"


# ---- 로그 ----

def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_kkachi", False) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "kkachi.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler._kkachi = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# ---- 버전 / 업데이트 (git) ----

def _git(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # 비밀번호를 묻느라 멈추지 않게
    )


def release() -> Optional[str]:
    """pyproject.toml 의 버전 (예: 1.0.1)"""
    import tomllib

    try:
        with open(REPO_DIR / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, ValueError):
        return None


def version() -> dict[str, Any]:
    r = _git("log", "-1", "--format=%h|%cI|%s")
    if r.returncode != 0:
        return {"commit": None, "date": None, "subject": None, "release": release()}
    commit, date, subject = r.stdout.strip().split("|", 2)
    return {"commit": commit, "date": date, "subject": subject, "release": release()}


def check_update() -> dict[str, Any]:
    """새 버전이 있는지 확인한다. 인터넷이 안 되면 available=None."""
    try:
        f = _git("fetch", "--quiet", "origin", BRANCH, timeout=30)
    except subprocess.TimeoutExpired:
        return {"available": None, "message": "업데이트 서버에 연결할 수 없어요. 인터넷 연결을 확인해 주세요."}
    if f.returncode != 0:
        log = logging.getLogger(__name__)
        log.warning("git fetch 실패: %s", f.stderr.strip())
        return {"available": None, "message": "업데이트를 확인할 수 없어요. 인터넷 연결을 확인해 주세요."}
    r = _git("log", "--format=%s", f"HEAD..origin/{BRANCH}")
    changes = [line for line in r.stdout.splitlines() if line.strip()]
    have = {n["id"] for n in load_notes()}
    notes = [n for n in load_notes(f"origin/{BRANCH}") if n["id"] not in have]
    return {"available": bool(changes), "changes": changes[:20], "count": len(changes), "notes": notes}


def apply_update(data_dir: Optional[Path] = None) -> dict[str, Any]:
    """새 코드 받기 + 라이브러리 맞추기. 성공하면 재시작이 필요하다."""
    log = logging.getLogger(__name__)
    if data_dir:
        unseen_notes(data_dir)  # 지금 버전까지의 노트는 본 걸로 적어둔다 → 업데이트 뒤엔 새 노트만 보여줌
    before = _git("rev-parse", "HEAD").stdout.strip()
    pull = _git("pull", "--ff-only", "--quiet", "origin", BRANCH, timeout=120)
    if pull.returncode != 0:
        log.error("git pull 실패: %s", pull.stderr.strip())
        return {"ok": False, "message": "새 버전을 받지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요."}
    uv = _find_uv()
    if uv:
        sync = subprocess.run([uv, "sync", "--frozen", "--no-dev"], cwd=REPO_DIR, capture_output=True, text=True, timeout=900)
        if sync.returncode != 0:
            log.error("uv sync 실패: %s", sync.stderr.strip()[-2000:])
            return {"ok": False, "message": "새 버전 준비 중 문제가 생겼어요. 로그를 보내주세요."}
    _rebuild_app_if_needed(before)
    return {"ok": True, "version": version()}


# ---- 업데이트 노트 (whatsnew.json) ----
# 커밋 메시지 대신 쓰는 사람이 알아보기 쉬운 설명. 새것이 앞에 오는 목록:
#   [{"id": "2026-10-07", "date": "2026-10-07", "title": "...", "items": [{"icon", "title", "text"}]}]

NOTES_FILE = "whatsnew.json"
SEEN_FILE = "whatsnew_seen.json"


def load_notes(ref: Optional[str] = None) -> list[dict[str, Any]]:
    """ref 가 없으면 지금 설치된 노트, 있으면 그 git 버전의 노트 (예: origin/main). 없거나 깨졌으면 []."""
    try:
        if ref is None:
            text = (REPO_DIR / NOTES_FILE).read_text(encoding="utf-8")
        else:
            r = _git("show", f"{ref}:{NOTES_FILE}")
            if r.returncode != 0:
                return []
            text = r.stdout
        notes = json.loads(text)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [n for n in notes if isinstance(n, dict) and n.get("id") and isinstance(n.get("items"), list)]


def unseen_notes(data_dir: Path) -> list[dict[str, Any]]:
    """아직 안 본 업데이트 노트. 처음 설치한 맥이면 지난 노트를 늘어놓지 않도록 모두 본 걸로 친다."""
    path = data_dir / SEEN_FILE
    notes = load_notes()
    try:
        seen = set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        # 기록이 없음: 처음 설치 또는 노트 기능이 생기기 전 버전에서 업데이트한 직후.
        # 업데이트 직후라면 git 이 남겨 둔 ORIG_HEAD(업데이트 전 버전)의 노트까지만 본 걸로 친다.
        if _git("rev-parse", "--verify", "--quiet", "ORIG_HEAD").returncode == 0:
            seen = {n["id"] for n in load_notes("ORIG_HEAD")}
        else:
            seen = {n["id"] for n in notes}
        _write_seen(path, seen)
    return [n for n in notes if n["id"] not in seen]


def mark_notes_seen(data_dir: Path) -> None:
    _write_seen(data_dir / SEEN_FILE, {n["id"] for n in load_notes()})


def _write_seen(path: Path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


APP_FILES = ("assets/kkachi-launcher", "assets/AppIcon.icns", "scripts/build_app.sh")


def _rebuild_app_if_needed(before: str) -> None:
    """실행기나 아이콘이 바뀐 업데이트면 까치녹음기.app 도 새로 만든다 (다음 실행부터 적용)."""
    changed = _git("diff", "--name-only", before, "HEAD", "--", *APP_FILES).stdout.strip()
    app_path_file = Path.home() / "Library/Application Support/KkachiNogeumgi/app_path"
    if not changed or not app_path_file.exists():
        return
    dest = str(Path(app_path_file.read_text().strip()).parent)
    r = subprocess.run(["bash", str(REPO_DIR / "scripts/build_app.sh"), str(REPO_DIR), dest],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        logging.getLogger(__name__).error("앱 다시 만들기 실패: %s", r.stderr.strip())


def _find_uv() -> Optional[str]:
    for p in (Path.home() / ".local/bin/uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if p.exists():
            return str(p)
    return None


# ---- 종료 / 재시작 ----

def shutdown_soon(delay: float = 0.5) -> None:
    """응답을 보낸 뒤 서버를 정상 종료한다 (uvicorn 이 SIGTERM 을 받아 정리)."""
    def _later():
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_later, daemon=True).start()


def restart_soon(delay: float = 0.8) -> None:
    """같은 명령으로 서버 프로세스를 새로 띄운다 (업데이트 후). 새 코드·라이브러리로 다시 시작된다."""
    def _later():
        time.sleep(delay)
        os.chdir(REPO_DIR)
        python = str(REPO_DIR / ".venv" / "bin" / "python")
        if not Path(python).exists():
            python = sys.executable
        os.execv(python, [python, "-m", "uvicorn", "app.main:app",
                          "--host", "127.0.0.1", "--port", os.environ.get("KKACHI_PORT", "8765")])
    threading.Thread(target=_later, daemon=True).start()


# ---- 전원 ----

def parse_pmset(out: str) -> dict[str, Any]:
    """`pmset -g batt` 출력 → {on_ac, percent}. 배터리가 없는 맥(데스크톱)은 percent=None."""
    import re
    on_ac = "AC Power" in out
    m = re.search(r"(\d+)%", out)
    return {"on_ac": on_ac or m is None, "percent": int(m.group(1)) if m else None}


def power_status() -> dict[str, Any]:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {"on_ac": True, "percent": None}
    return parse_pmset(out)


# ---- 선물 설정 (저장소 밖 gift.json: 이름) ----

def load_gift(data_dir: Path) -> dict[str, str]:
    import json
    try:
        raw = json.loads((data_dir / "gift.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for key, limit in (("name", 20),):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:limit]
    return out


# ---- 문제 신고용 로그 묶음 ----

def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def system_info(data_dir: Path, db_stats: dict[str, Any]) -> str:
    mem = _sysctl("hw.memsize")
    try:
        usage = os.statvfs(data_dir)
        free_gb = usage.f_bavail * usage.f_frsize / 1e9
    except OSError:
        free_gb = -1
    v = version()
    lines = [
        f"앱 버전: {v['commit']} ({v['date']}) {v['subject']}",
        f"macOS: {platform.mac_ver()[0]}  칩: {_sysctl('machdep.cpu.brand_string')}",
        f"메모리: {int(mem) / 2**30:.0f}GB" if mem.isdigit() else "메모리: ?",
        f"남은 저장 공간: {free_gb:.1f}GB",
        f"Python: {sys.version.split()[0]}",
        f"녹음 수: {db_stats}",
        f"만든 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return "\n".join(lines) + "\n"


def export_logs(data_dir: Path, db_stats: dict[str, Any], dest_dir: Optional[Path] = None) -> Path:
    """로그와 기기 정보만 묶는다 (녹음·받아쓴 내용은 넣지 않음). 기본 위치는 바탕화면."""
    dest_dir = dest_dir or (Path.home() / "Desktop")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"까치녹음기-로그-{time.strftime('%Y%m%d-%H%M')}.zip"
    logs_dir = data_dir / "logs"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("기기정보.txt", system_info(data_dir, db_stats))
        if logs_dir.exists():
            for f in sorted(logs_dir.glob("*.log*")):
                z.write(f, f"logs/{f.name}")
    return path
