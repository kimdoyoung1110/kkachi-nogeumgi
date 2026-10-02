"""SQLite 저장소. 녹음 목록, 처리 상태, 화자 이름, 전사 문단(+ 한국어 부분 검색용 FTS5 trigram)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    created_at      REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'upload',   -- upload / record
    file_name       TEXT NOT NULL,                    -- recordings/<id>/ 안의 원본 파일 이름
    duration        REAL,
    language        TEXT,                             -- NULL=자동(한·영 혼합), 'ko', 'en'
    num_speakers    INTEGER,                          -- NULL=자동
    hotwords        TEXT NOT NULL DEFAULT '[]',       -- JSON 배열
    replacements    TEXT NOT NULL DEFAULT '{}',       -- JSON 객체
    subject         TEXT,                             -- 과목 (용어 힌트 묶음)
    status          TEXT NOT NULL DEFAULT 'queued',   -- queued / processing / done / failed
    stage           TEXT,
    progress        REAL NOT NULL DEFAULT 0,          -- 0~1
    error           TEXT,
    processing_secs REAL
);

CREATE TABLE IF NOT EXISTS speakers (
    recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (recording_id, idx)
);

CREATE TABLE IF NOT EXISTS utterances (
    id           INTEGER PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    speaker      INTEGER NOT NULL,
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    language     TEXT NOT NULL,
    text         TEXT NOT NULL,
    words        TEXT NOT NULL DEFAULT '[]'           -- JSON [[text, start, end], ...]
);
CREATE INDEX IF NOT EXISTS utterances_rec ON utterances(recording_id, idx);

CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5(
    text, content='utterances', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS utterances_ai AFTER INSERT ON utterances BEGIN
    INSERT INTO utterances_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS utterances_ad AFTER DELETE ON utterances BEGIN
    INSERT INTO utterances_fts(utterances_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS utterances_au AFTER UPDATE OF text ON utterances BEGIN
    INSERT INTO utterances_fts(utterances_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO utterances_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

RECORDING_FIELDS = (
    "id", "title", "created_at", "source", "file_name", "duration", "language", "num_speakers",
    "subject", "status", "stage", "progress", "error", "processing_secs",
)


class Database:
    def __init__(self, path: Path = config.DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- recordings ----

    def create_recording(
        self,
        title: str,
        file_name: str,
        source: str = "upload",
        language: Optional[str] = None,
        num_speakers: Optional[int] = None,
        hotwords: Optional[list[str]] = None,
        replacements: Optional[dict[str, str]] = None,
        subject: Optional[str] = None,
        rec_id: Optional[str] = None,
    ) -> dict[str, Any]:
        rec_id = rec_id or uuid.uuid4().hex[:12]
        with self.conn() as c:
            c.execute(
                "INSERT INTO recordings (id, title, created_at, source, file_name, language, num_speakers,"
                " hotwords, replacements, subject) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec_id, title, time.time(), source, file_name, language, num_speakers,
                 json.dumps(hotwords or [], ensure_ascii=False),
                 json.dumps(replacements or {}, ensure_ascii=False), subject),
            )
        return self.get_recording(rec_id)

    def get_recording(self, rec_id: str) -> Optional[dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        return _recording(row) if row else None

    def list_recordings(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM recordings ORDER BY created_at DESC").fetchall()
        return [_recording(r) for r in rows]

    def update_recording(self, rec_id: str, **fields: Any) -> None:
        if not fields:
            return
        for k in ("hotwords", "replacements"):
            if k in fields:
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE recordings SET {cols} WHERE id = ?", (*fields.values(), rec_id))

    def delete_recording(self, rec_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))

    def ids_with_status(self, *statuses: str) -> list[str]:
        q = ",".join("?" * len(statuses))
        with self.conn() as c:
            rows = c.execute(
                f"SELECT id FROM recordings WHERE status IN ({q}) ORDER BY created_at", statuses
            ).fetchall()
        return [r["id"] for r in rows]

    # ---- 전사 결과 ----

    def save_transcript(self, rec_id: str, utterances: list[Any]) -> None:
        """기존 결과를 지우고 새로 저장한다. 화자 이름은 이미 바꾼 게 있으면 유지."""
        with self.conn() as c:
            c.execute("DELETE FROM utterances WHERE recording_id = ?", (rec_id,))
            for i, u in enumerate(utterances):
                c.execute(
                    "INSERT INTO utterances (recording_id, idx, speaker, start, end, language, text, words)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (rec_id, i, u.speaker, u.start, u.end, u.language, u.text,
                     json.dumps([[w.text, round(w.start, 3), round(w.end, 3)] for w in u.words],
                                ensure_ascii=False)),
                )
            for spk in sorted({u.speaker for u in utterances}):
                c.execute(
                    "INSERT OR IGNORE INTO speakers (recording_id, idx, name) VALUES (?,?,?)",
                    (rec_id, spk, f"화자 {spk + 1}"),
                )

    def get_transcript(self, rec_id: str) -> dict[str, Any]:
        with self.conn() as c:
            speakers = c.execute(
                "SELECT idx, name FROM speakers WHERE recording_id = ? ORDER BY idx", (rec_id,)
            ).fetchall()
            utts = c.execute(
                "SELECT id, idx, speaker, start, end, language, text, words FROM utterances"
                " WHERE recording_id = ? ORDER BY idx", (rec_id,)
            ).fetchall()
        return {
            "speakers": [dict(s) for s in speakers],
            "utterances": [{**dict(u), "words": json.loads(u["words"])} for u in utts],
        }


def _recording(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in RECORDING_FIELDS}
    d["hotwords"] = json.loads(row["hotwords"])
    d["replacements"] = json.loads(row["replacements"])
    return d
