"""FastAPI 서버. 화면(web/)과 API(/api/...)를 같이 제공한다.

    uv run uvicorn app.main:app --port 8765
"""

from __future__ import annotations

import logging
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.db import Database
from app.worker import STAGE_LABELS, Worker

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UPLOAD_CHUNK = 1024 * 1024


class WebFiles(StaticFiles):
    """화면 파일은 매번 최신인지 확인하게 한다 (업데이트 후 예전 화면이 보이는 것 방지)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def parse_terms(text: str) -> list[str]:
    """'hash table, collision\\nlinked list' → ['hash table', 'collision', 'linked list']"""
    return [t.strip() for t in re.split(r"[,\n]", text or "") if t.strip()]


def parse_replacements(text: str) -> dict[str, str]:
    """'컬리전=collision\\n세프리 체인잉 = separate chaining' → dict. '→' 도 허용."""
    out: dict[str, str] = {}
    for line in re.split(r"[,\n]", text or ""):
        m = re.match(r"\s*(.+?)\s*(?:=|→|->)\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".bin"


def create_app(
    data_dir: Optional[Path] = None,
    pipeline_factory: Optional[Callable[[], object]] = None,
) -> FastAPI:
    data_dir = data_dir or config.DATA_DIR
    recordings_dir = data_dir / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)

    if pipeline_factory is None:
        def pipeline_factory():
            from app.pipeline import Pipeline  # 무거운 모델 라이브러리는 처음 처리할 때 불러온다
            return Pipeline()

    db = Database(data_dir / "kkachi.db")
    worker = Worker(db, recordings_dir, pipeline_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        yield
        worker.stop()

    app = FastAPI(title="까치녹음기", lifespan=lifespan)
    app.state.db = db
    app.state.worker = worker

    def get_or_404(rec_id: str) -> dict:
        rec = db.get_recording(rec_id)
        if rec is None:
            raise HTTPException(404, "녹음을 찾을 수 없어요.")
        return rec

    def with_label(rec: dict) -> dict:
        return {**rec, "stage_label": STAGE_LABELS.get(rec["stage"] or rec["status"], "")}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "data_dir": str(data_dir)}

    @app.get("/api/recordings")
    def list_recordings() -> list[dict]:
        return [with_label(r) for r in db.list_recordings()]

    @app.post("/api/recordings", status_code=201)
    async def upload(
        file: UploadFile = File(...),
        title: str = Form(""),
        language: str = Form("auto"),
        num_speakers: str = Form(""),
        hotwords: str = Form(""),
        replacements: str = Form(""),
        subject: str = Form(""),
        source: str = Form("upload"),
    ) -> dict:
        if language not in ("auto", "ko", "en"):
            raise HTTPException(400, "언어는 자동/한국어/영어 중에서 골라주세요.")
        n_spk = int(num_speakers) if num_speakers.strip().isdigit() else None
        if n_spk is not None and not 1 <= n_spk <= 8:
            raise HTTPException(400, "화자 수는 1~8명까지 지정할 수 있어요.")

        file_name = "original" + _safe_suffix(file.filename)
        rec = db.create_recording(
            title=title.strip() or Path(file.filename or "녹음").stem,
            file_name=file_name,
            source="record" if source == "record" else "upload",
            language=None if language == "auto" else language,
            num_speakers=n_spk,
            hotwords=parse_terms(hotwords),
            replacements=parse_replacements(replacements),
            subject=subject.strip() or None,
        )
        if rec["subject"]:
            # 과목을 정했으면 이번에 쓴 용어를 그 과목에 저장해서 다음 녹음에 다시 채워준다
            db.save_subject(rec["subject"], rec["hotwords"], rec["replacements"])

        dest_dir = recordings_dir / rec["id"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(dest_dir / file_name, "wb") as out:
                while chunk := await file.read(UPLOAD_CHUNK):
                    out.write(chunk)
        except OSError:
            db.delete_recording(rec["id"])
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise HTTPException(507, "저장 공간이 부족해서 파일을 저장하지 못했어요.")

        worker.enqueue(rec["id"])
        return with_label(db.get_recording(rec["id"]))

    @app.get("/api/recordings/{rec_id}")
    def detail(rec_id: str) -> dict:
        rec = get_or_404(rec_id)
        return {**with_label(rec), **db.get_transcript(rec_id)}

    @app.get("/api/recordings/{rec_id}/status")
    def status(rec_id: str) -> dict:
        rec = get_or_404(rec_id)
        return {k: rec[k] for k in ("id", "status", "stage", "progress", "error")} | {
            "stage_label": with_label(rec)["stage_label"]
        }

    @app.post("/api/recordings/{rec_id}/retry")
    def retry(rec_id: str) -> dict:
        rec = get_or_404(rec_id)
        if rec["status"] in ("queued", "processing"):
            raise HTTPException(409, "이미 처리 중이에요.")
        worker.enqueue(rec_id)
        return with_label(db.get_recording(rec_id))

    @app.delete("/api/recordings/{rec_id}", status_code=204)
    def delete(rec_id: str) -> None:
        get_or_404(rec_id)
        db.delete_recording(rec_id)
        shutil.rmtree(recordings_dir / rec_id, ignore_errors=True)

    @app.get("/api/recordings/{rec_id}/audio")
    def audio(rec_id: str) -> FileResponse:
        rec = get_or_404(rec_id)
        path = recordings_dir / rec_id / rec["file_name"]
        if not path.exists():
            raise HTTPException(404, "녹음 파일이 없어요.")
        return FileResponse(path)

    @app.get("/api/subjects")
    def subjects() -> list[dict]:
        return db.list_subjects()

    @app.delete("/api/subjects/{name}", status_code=204)
    def delete_subject(name: str) -> None:
        db.delete_subject(name)

    if (WEB_DIR / "index.html").exists():
        app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")

    return app


app = create_app()
