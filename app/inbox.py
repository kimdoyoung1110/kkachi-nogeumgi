"""아이폰에서 보낸 파일 받기: iCloud Drive 의 '까치녹음기' 폴더를 지켜보다가 새 소리·영상 파일을 가져온다.

아이폰 단축어 '까치녹음기로 보내기'가 영상에서 소리만 뽑아 이 폴더에 저장한다.
가져온 파일은 같은 폴더의 '가져온 파일' 안으로 옮겨서 두 번 가져오지 않게 한다.

    ~/Library/Mobile Documents/com~apple~CloudDocs/까치녹음기/
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

ICLOUD_DRIVE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
FOLDER_NAME = "까치녹음기"
DONE_FOLDER = "가져온 파일"
SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".mp4", ".mov", ".m4v", ".webm", ".ogg", ".flac", ".qta"}
SETTLE_SECONDS = 5   # 이만큼 그대로인 파일만 가져온다 (iCloud 가 아직 내려받는 중일 수 있어서)


class Inbox:
    def __init__(self, root: Path, on_file: Callable[[Path], None], interval: float = 15):
        self.root = root
        self.on_file = on_file
        self.interval = interval
        self.status = "starting"   # ok / no-icloud / permission
        self.imported = 0
        self.last_import: Optional[float] = None
        self._seen: dict[str, tuple[int, float]] = {}
        self._asked_download: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="kkachi-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("아이폰 보낸 파일 확인 실패")
            self._stop.wait(self.interval)

    def scan(self, now: Optional[float] = None) -> int:
        """새 파일을 가져오고 가져온 개수를 돌려준다."""
        now = now or time.time()
        if not self.root.parent.exists():
            self.status = "no-icloud"
            return 0
        try:
            self.root.mkdir(exist_ok=True)
            entries = sorted(self.root.iterdir())
        except PermissionError:
            self.status = "permission"
            return 0
        self.status = "ok"

        count = 0
        for p in entries:
            name = p.name
            if name.startswith(".") and name.endswith(".icloud"):
                # 아직 맥으로 안 내려온 파일 ('.이름.m4a.icloud') → 내려받아 달라고 한 번 부탁한다
                real = name[1:-len(".icloud")]
                if Path(real).suffix.lower() in SUFFIXES and real not in self._asked_download:
                    self._asked_download.add(real)
                    subprocess.run(["brctl", "download", str(p)], capture_output=True, check=False)
                continue
            if name.startswith(".") or not p.is_file() or p.suffix.lower() not in SUFFIXES:
                continue
            st = p.stat()
            sig = (st.st_size, st.st_mtime)
            if self._seen.get(name) != sig or now - st.st_mtime < SETTLE_SECONDS or st.st_size == 0:
                self._seen[name] = sig   # 다음 확인 때도 그대로면 가져온다
                continue
            try:
                self.on_file(p)
            except Exception:
                log.exception("아이폰에서 보낸 파일을 가져오지 못함: %s", name)
                continue
            self._seen.pop(name, None)
            _move_done(p)
            count += 1
            self.imported += 1
            self.last_import = now
            log.info("아이폰에서 보낸 파일 가져옴: %s", name)
        return count


def _move_done(p: Path) -> None:
    done = p.parent / DONE_FOLDER
    try:
        done.mkdir(exist_ok=True)
        dest = done / p.name
        n = 2
        while dest.exists():
            dest = done / f"{p.stem} ({n}){p.suffix}"
            n += 1
        shutil.move(str(p), dest)
    except OSError:
        log.warning("가져온 파일을 옮기지 못함 (출처 표시로 다시 가져오지는 않음): %s", p.name)
