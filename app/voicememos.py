"""아이폰 '음성 메모' 가져오기.

아이폰과 맥이 같은 iCloud 계정이고 음성 메모 iCloud 동기화가 켜져 있으면, 아이폰에서 녹음한 파일이
맥의 아래 폴더로 내려온다. 제목·녹음 시각·길이는 같은 폴더의 CloudRecordings.db 에 있다.

    ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/

이 폴더는 macOS 가 보호하는 곳이라 까치녹음기에 '전체 디스크 접근 권한'이 있어야 읽을 수 있다.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared"
AUDIO_SUFFIXES = (".m4a", ".qta", ".mp4", ".caf", ".wav")
APPLE_EPOCH = 978307200  # 2001-01-01 (Core Data 시간 기준)


class NoPermission(Exception):
    pass


def recordings_dir(root: Path = ROOT) -> Path:
    return root / "Recordings"


def list_memos(root: Path = ROOT, limit: int = 200) -> Optional[list[dict[str, Any]]]:
    """최근 음성 메모 목록 (새것부터). 음성 메모를 한 번도 안 쓴 맥이면 None.
    권한이 없으면 NoPermission."""
    rec_dir = recordings_dir(root)
    try:
        if not rec_dir.exists():
            # 권한이 없어도 exists() 는 False 가 될 수 있다 → 상위 폴더로 구분
            if root.exists():
                list(root.iterdir())
            return None
        files = [p for p in rec_dir.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES]
    except PermissionError as e:
        raise NoPermission() from e

    rows = _read_db(rec_dir / "CloudRecordings.db")
    meta = {r["name"]: r for r in rows if r["name"]}
    items = []
    present = set()
    for p in files:
        m = meta.get(p.name, {})
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < 1024:  # 아직 iCloud 에서 내려오는 중이거나 빈 파일
            continue
        present.add(p.name)
        items.append({
            "key": m.get("uid") or p.name,
            "file": p.name,
            "title": m.get("title") or "음성 메모",
            "date": m.get("date") or st.st_mtime,
            "duration": m.get("duration"),
            "size": st.st_size,
            "available": True,
        })
    # 음성 메모 목록(DB)에는 있는데 파일이 아직 맥에 없는 것 (iCloud 에서 안 내려옴) → 보여주기만
    for r in rows:
        if r["name"] in present or not (r["uid"] or r["name"]):
            continue
        items.append({
            "key": r["uid"] or r["name"],
            "file": r["name"],
            "title": r["title"] or "음성 메모",
            "date": r["date"] or 0,
            "duration": r["duration"],
            "size": 0,
            "available": False,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:limit]


def find(key: str, root: Path = ROOT) -> Optional[dict[str, Any]]:
    for m in list_memos(root, limit=100_000) or []:
        if m["key"] == key:
            return m
    return None


def _read_db(path: Path) -> list[dict[str, Any]]:
    """CloudRecordings.db 의 음성 메모들 (파일 이름·제목·날짜·길이). 형식이 바뀌었거나 없으면 빈 목록.
    아직 맥에 안 내려온 녹음은 파일 이름(ZPATH)이 비어 있을 수 있다."""
    if not path.exists():
        return []
    # 음성 메모 앱이 쓰는 중일 수 있어서 복사본을 읽는다 (-wal 까지 같이)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    shutil.copy2(src, Path(tmp) / (path.name + suffix))
            c = sqlite3.connect(Path(tmp) / path.name)
            c.row_factory = sqlite3.Row
            cols = {r["name"] for r in c.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
            if "ZPATH" not in cols:
                return []
            pick = [x for x in ("ZENCRYPTEDTITLE", "ZCUSTOMLABEL", "ZDATE", "ZDURATION", "ZUNIQUEID") if x in cols]
            rows = c.execute(f"SELECT ZPATH, {', '.join(pick)} FROM ZCLOUDRECORDING").fetchall()
            c.close()
        except (sqlite3.Error, OSError):
            return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "name": Path(d["ZPATH"]).name if d.get("ZPATH") else None,
            "title": (d.get("ZENCRYPTEDTITLE") or d.get("ZCUSTOMLABEL") or "").strip() or None,
            "date": d["ZDATE"] + APPLE_EPOCH if d.get("ZDATE") else None,
            "duration": d.get("ZDURATION"),
            "uid": d.get("ZUNIQUEID"),
        })
    return out

