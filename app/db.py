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
    processing_secs REAL,
    finished_at     REAL                              -- 마지막으로 받아쓰기가 끝난(완료/실패) 시각. 알림용
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
    words        TEXT NOT NULL DEFAULT '[]',          -- JSON [[text, start, end], ...] (직접 고치면 비움)
    busy         INTEGER NOT NULL DEFAULT 0           -- 1 = 다시 받아쓰는 중
);
CREATE INDEX IF NOT EXISTS utterances_rec ON utterances(recording_id, idx);

-- 과목별로 저장해 두는 용어 힌트 / 바꾸기 사전 (다음 녹음에 자동으로 채워짐)
CREATE TABLE IF NOT EXISTS subjects (
    name         TEXT PRIMARY KEY,
    hotwords     TEXT NOT NULL DEFAULT '[]',
    replacements TEXT NOT NULL DEFAULT '{}',
    updated_at   REAL NOT NULL
);

-- 녹음 중(또는 결과 화면에서) '중요'로 표시한 시점. 받아쓰기를 다시 해도 시간 기준이라 그대로 남는다
CREATE TABLE IF NOT EXISTS marks (
    id           INTEGER PRIMARY KEY,
    recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    t            REAL NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS marks_rec ON marks(recording_id, t);

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
    "subject", "status", "stage", "progress", "error", "processing_secs", "finished_at",
)


class Database:
    def __init__(self, path: Path = config.DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            _migrate(c)

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

    # ---- 과목 ----

    def list_subjects(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM subjects ORDER BY updated_at DESC").fetchall()
        return [
            {"name": r["name"], "hotwords": json.loads(r["hotwords"]),
             "replacements": json.loads(r["replacements"])}
            for r in rows
        ]

    def save_subject(self, name: str, hotwords: list[str], replacements: dict[str, str]) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO subjects (name, hotwords, replacements, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET hotwords=excluded.hotwords,"
                " replacements=excluded.replacements, updated_at=excluded.updated_at",
                (name, json.dumps(hotwords, ensure_ascii=False),
                 json.dumps(replacements, ensure_ascii=False), time.time()),
            )

    def delete_subject(self, name: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM subjects WHERE name = ?", (name,))

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

    def get_utterance(self, utt_id: int) -> Optional[dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM utterances WHERE id = ?", (utt_id,)).fetchone()
        return _utterance(row) if row else None

    def update_utterance(self, utt_id: int, **fields: Any) -> None:
        if "words" in fields:
            fields["words"] = json.dumps(fields["words"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE utterances SET {cols} WHERE id = ?", (*fields.values(), utt_id))

    def delete_utterance(self, utt_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM utterances WHERE id = ?", (utt_id,))

    def clear_busy(self) -> None:
        with self.conn() as c:
            c.execute("UPDATE utterances SET busy = 0 WHERE busy = 1")

    def rename_speaker(self, rec_id: str, idx: int, name: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO speakers (recording_id, idx, name) VALUES (?,?,?)"
                " ON CONFLICT(recording_id, idx) DO UPDATE SET name = excluded.name",
                (rec_id, idx, name),
            )

    def add_speaker(self, rec_id: str, name: Optional[str] = None) -> dict[str, Any]:
        with self.conn() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(idx), -1) + 1 AS n FROM speakers WHERE recording_id = ?", (rec_id,)
            ).fetchone()
            idx = row["n"]
            name = name or f"화자 {idx + 1}"
            c.execute("INSERT INTO speakers (recording_id, idx, name) VALUES (?,?,?)", (rec_id, idx, name))
        return {"idx": idx, "name": name}

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        """모든 녹음의 문단에서 글자 그대로(부분 일치, 영문 대소문자 무시) 찾는다."""
        q = query.strip()
        if not q:
            return []
        base = (
            "SELECT u.id, u.recording_id, u.speaker, u.start, u.text, r.title, r.created_at,"
            " COALESCE(s.name, '화자 ' || (u.speaker + 1)) AS speaker_name"
            " FROM utterances u JOIN recordings r ON r.id = u.recording_id"
            " LEFT JOIN speakers s ON s.recording_id = u.recording_id AND s.idx = u.speaker"
        )
        order = " ORDER BY r.created_at DESC, u.idx LIMIT ?"
        with self.conn() as c:
            if len(q) >= 3:
                # trigram 색인은 3글자 이상부터. 따옴표로 감싸 특수문자도 글자로 취급
                phrase = '"' + q.replace('"', '""') + '"'
                rows = c.execute(
                    base + " WHERE u.id IN (SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?)" + order,
                    (phrase, limit),
                ).fetchall()
            else:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = c.execute(base + " WHERE u.text LIKE ? ESCAPE '\\'" + order, (like, limit)).fetchall()
        return [dict(r) for r in rows]

    def merge_speaker(self, rec_id: str, src: int, dst: int) -> None:
        """src 화자의 문단을 모두 dst 로 옮기고, 그 결과 붙어 있게 된 같은 화자 문단을 하나로 합친다."""
        with self.conn() as c:
            c.execute("UPDATE utterances SET speaker = ? WHERE recording_id = ? AND speaker = ?", (dst, rec_id, src))
            c.execute("DELETE FROM speakers WHERE recording_id = ? AND idx = ?", (rec_id, src))
            rows = c.execute(
                "SELECT id, speaker, end, text, words, busy FROM utterances WHERE recording_id = ? ORDER BY start, idx",
                (rec_id,),
            ).fetchall()
            keep = None
            for r in rows:
                if (keep is not None and r["speaker"] == keep["speaker"] == dst
                        and not keep["busy"] and not r["busy"]):
                    keep["text"] = keep["text"] + " " + r["text"]
                    keep["end"] = max(keep["end"], r["end"])
                    keep["words"] = json.dumps(json.loads(keep["words"]) + json.loads(r["words"]), ensure_ascii=False)
                    c.execute("UPDATE utterances SET text = ?, end = ?, words = ? WHERE id = ?",
                              (keep["text"], keep["end"], keep["words"], keep["id"]))
                    c.execute("DELETE FROM utterances WHERE id = ?", (r["id"],))
                else:
                    keep = dict(r)
            # 순서 번호 다시 매기기
            ids = c.execute("SELECT id FROM utterances WHERE recording_id = ? ORDER BY start, idx", (rec_id,)).fetchall()
            for i, r in enumerate(ids):
                c.execute("UPDATE utterances SET idx = ? WHERE id = ?", (i, r["id"]))

    # ---- 중요 표시 ----

    def add_mark(self, rec_id: str, t: float) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO marks (recording_id, t, created_at) VALUES (?,?,?)", (rec_id, t, time.time()))

    def delete_marks(self, rec_id: str, start: float, end: float) -> int:
        with self.conn() as c:
            return c.execute(
                "DELETE FROM marks WHERE recording_id = ? AND t >= ? AND t <= ?", (rec_id, start, end)
            ).rowcount

    def list_marks(self, rec_id: str) -> list[float]:
        with self.conn() as c:
            rows = c.execute("SELECT t FROM marks WHERE recording_id = ? ORDER BY t", (rec_id,)).fetchall()
        return [r["t"] for r in rows]

    def get_transcript(self, rec_id: str) -> dict[str, Any]:
        with self.conn() as c:
            speakers = c.execute(
                "SELECT idx, name FROM speakers WHERE recording_id = ? ORDER BY idx", (rec_id,)
            ).fetchall()
            utts = c.execute(
                "SELECT * FROM utterances WHERE recording_id = ? ORDER BY idx", (rec_id,)
            ).fetchall()
        return {
            "speakers": [dict(s) for s in speakers],
            "utterances": [_utterance(u) for u in utts],
            "marks": self.list_marks(rec_id),
        }


def _migrate(c: sqlite3.Connection) -> None:
    """예전 버전으로 만든 DB에 새 칸을 추가한다."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(utterances)")}
    if "busy" not in cols:
        c.execute("ALTER TABLE utterances ADD COLUMN busy INTEGER NOT NULL DEFAULT 0")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(recordings)")}
    if "finished_at" not in cols:
        c.execute("ALTER TABLE recordings ADD COLUMN finished_at REAL")


def _utterance(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in ("id", "recording_id", "idx", "speaker", "start", "end", "language", "text")}
    d["words"] = json.loads(row["words"])
    d["busy"] = bool(row["busy"])
    return d


def _recording(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in RECORDING_FIELDS}
    d["hotwords"] = json.loads(row["hotwords"])
    d["replacements"] = json.loads(row["replacements"])
    return d
