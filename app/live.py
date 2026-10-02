"""브라우저 녹음 세션: 10초마다 오는 조각을 저장하고, 끝나면 하나의 파일로 합친다.

조각은 chunks/000001.part 처럼 번호별 파일로 둔다. 같은 번호가 다시 오면 덮어쓰므로
네트워크 재시도에도 안전하고, 브라우저가 갑자기 꺼져도 그때까지 받은 조각은 남는다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import imageio_ffmpeg

# 마지막 조각 이후 이만큼 아무것도 안 오면 '끊긴 녹음'으로 본다 (조각은 10초마다 옴)
STALLED_SECONDS = 45
MAX_CHUNK_BYTES = 50 * 1024 * 1024
CONTAINERS = {"webm": ".webm", "mp4": ".mp4", "ogg": ".ogg"}


def chunks_dir(rec_dir: Path) -> Path:
    return rec_dir / "chunks"


def save_chunk(rec_dir: Path, seq: int, data: bytes) -> None:
    d = chunks_dir(rec_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{seq:06d}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / f"{seq:06d}.part")  # 반쯤 쓰인 조각이 남지 않게


def chunk_count(rec_dir: Path) -> int:
    d = chunks_dir(rec_dir)
    return len(list(d.glob("*.part"))) if d.exists() else 0


def last_activity(rec_dir: Path) -> float:
    d = chunks_dir(rec_dir)
    parts = list(d.glob("*.part")) if d.exists() else []
    times = [p.stat().st_mtime for p in parts] or [rec_dir.stat().st_mtime if rec_dir.exists() else 0]
    return max(times)


def is_stalled(rec_dir: Path, now: Optional[float] = None) -> bool:
    return (now or time.time()) - last_activity(rec_dir) > STALLED_SECONDS


def assemble(rec_dir: Path, ext: str) -> str:
    """조각을 순서대로 이어 붙이고, 재생·탐색이 되도록 ffmpeg 로 다시 포장한다. 원본 파일 이름을 돌려준다."""
    if ext not in CONTAINERS.values():
        ext = ".webm"
    parts = sorted(chunks_dir(rec_dir).glob("*.part"))
    if not parts:
        raise ValueError("저장된 녹음 조각이 없어요.")
    joined = rec_dir / f"joined{ext}"
    with open(joined, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)

    # MediaRecorder 결과물은 길이 정보가 없어 재생 바에서 탐색이 안 됨 → 그대로 복사하며 다시 포장
    final = rec_dir / f"original{ext}"
    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-y",
         "-i", str(joined), "-c", "copy", str(final)],
        capture_output=True,
    )
    if proc.returncode == 0 and final.exists() and final.stat().st_size > 0:
        joined.unlink()
    else:
        joined.replace(final)  # 다시 포장에 실패해도 녹음은 살린다
    shutil.rmtree(chunks_dir(rec_dir), ignore_errors=True)
    return final.name


class AwakeKeeper:
    """녹음 중인 세션마다 맥이 잠들지 않게 한다. 조각이 끊기면 알아서 풀어준다."""

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="kkachi-awake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for p in self._procs.values():
                p.terminate()
            self._procs.clear()

    def hold(self, rec_id: str) -> None:
        with self._lock:
            if rec_id in self._procs:
                return
            try:
                self._procs[rec_id] = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                pass

    def release(self, rec_id: str) -> None:
        with self._lock:
            p = self._procs.pop(rec_id, None)
        if p:
            p.terminate()

    def _watch(self) -> None:
        while not self._stop.wait(15):
            with self._lock:
                ids = list(self._procs)
            for rec_id in ids:
                if is_stalled(self.recordings_dir / rec_id):
                    self.release(rec_id)
