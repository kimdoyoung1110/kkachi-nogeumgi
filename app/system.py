"""앱 자체 관리: 로그, 버전, 업데이트, 문제 신고용 로그 묶음, 종료/재시작."""

from __future__ import annotations

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


def version() -> dict[str, Any]:
    r = _git("log", "-1", "--format=%h|%cI|%s")
    if r.returncode != 0:
        return {"commit": None, "date": None, "subject": None}
    commit, date, subject = r.stdout.strip().split("|", 2)
    return {"commit": commit, "date": date, "subject": subject}


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
    return {"available": bool(changes), "changes": changes[:20], "count": len(changes)}


def apply_update() -> dict[str, Any]:
    """새 코드 받기 + 라이브러리 맞추기. 성공하면 재시작이 필요하다."""
    log = logging.getLogger(__name__)
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
    return {"ok": True, "version": version()}


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
