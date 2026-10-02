"""FastAPI 서버. 화면(web/)과 API(/api/...)를 같이 제공한다.

    uv run uvicorn app.main:app --port 8765
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, export, live, system
from app.db import Database
from app.worker import STAGE_LABELS, Worker

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UPLOAD_CHUNK = 1024 * 1024


class RecordingPatch(BaseModel):
    title: str


class SpeakerPatch(BaseModel):
    name: str


class SpeakerMerge(BaseModel):
    into: int


class UtterancePatch(BaseModel):
    text: Optional[str] = None
    speaker: Optional[int] = None


class RetranscribeRequest(BaseModel):
    language: str = "auto"  # auto / ko / en


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
    system.setup_logging(data_dir / "logs")

    if pipeline_factory is None:
        def pipeline_factory():
            from app.pipeline import Pipeline  # 무거운 모델 라이브러리는 처음 처리할 때 불러온다
            return Pipeline()

    db = Database(data_dir / "kkachi.db")
    worker = Worker(db, recordings_dir, pipeline_factory)
    awake = live.AwakeKeeper(recordings_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        awake.start()
        yield
        awake.stop()
        worker.stop()

    app = FastAPI(title="까치녹음기", lifespan=lifespan)
    app.state.db = db
    app.state.worker = worker
    app.state.log_export_dir = None   # None = 바탕화면 (테스트에서 바꿈)
    app.state.reveal_files = True

    def get_or_404(rec_id: str) -> dict:
        rec = db.get_recording(rec_id)
        if rec is None:
            raise HTTPException(404, "녹음을 찾을 수 없어요.")
        return rec

    def with_label(rec: dict) -> dict:
        out = {**rec, "stage_label": STAGE_LABELS.get(rec["stage"] or rec["status"], "")}
        if rec["status"] == "recording":
            out["stalled"] = live.is_stalled(recordings_dir / rec["id"])
        return out

    @app.get("/api/health")
    def health() -> dict:
        # 실행기(.app)는 "app" 값으로 우리 서버가 맞는지 확인한다
        return {"ok": True, "app": "kkachi", "data_dir": str(data_dir)}

    # ---- 앱 관리 (종료 / 업데이트 / 로그) ----

    def busy_reasons() -> list[str]:
        reasons = []
        statuses = [r["status"] for r in db.list_recordings()]
        if worker.busy or "processing" in statuses or "queued" in statuses:
            reasons.append("받아쓰는 중인 녹음이 있어요")
        if any(r["status"] == "recording" and not live.is_stalled(recordings_dir / r["id"])
               for r in db.list_recordings()):
            reasons.append("녹음 중이에요")
        return reasons

    @app.get("/api/app")
    def app_info() -> dict:
        return {"version": system.version(), "busy": busy_reasons()}

    @app.post("/api/app/quit")
    def quit_app() -> dict:
        log.info("화면에서 앱 종료 요청")
        system.shutdown_soon()
        return {"ok": True}

    @app.get("/api/app/update")
    def update_check() -> dict:
        return {**system.check_update(), "version": system.version()}

    @app.post("/api/app/update")
    def update_apply() -> dict:
        reasons = busy_reasons()
        if reasons:
            raise HTTPException(409, f"{reasons[0]}. 끝난 뒤에 업데이트해 주세요.")
        result = system.apply_update()
        if not result["ok"]:
            raise HTTPException(500, result["message"])
        log.info("업데이트 완료, 다시 시작: %s", result["version"])
        system.restart_soon()
        return {**result, "restarting": True}

    @app.post("/api/app/logs")
    def save_logs() -> dict:
        stats: dict = {}
        for r in db.list_recordings():
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        path = system.export_logs(data_dir, stats, dest_dir=app.state.log_export_dir)
        if app.state.reveal_files:
            subprocess.run(["open", "-R", str(path)], check=False)  # Finder 에서 파일 보여주기
        return {"path": str(path), "name": path.name}

    @app.get("/api/recordings")
    def list_recordings() -> list[dict]:
        return [with_label(r) for r in db.list_recordings()]

    def parse_options(language: str, num_speakers: str, hotwords: str, replacements: str, subject: str) -> dict:
        if language not in ("auto", "ko", "en"):
            raise HTTPException(400, "언어는 자동/한국어/영어 중에서 골라주세요.")
        n_spk = int(num_speakers) if num_speakers.strip().isdigit() else None
        if n_spk is not None and not 1 <= n_spk <= 8:
            raise HTTPException(400, "화자 수는 1~8명까지 지정할 수 있어요.")
        return dict(
            language=None if language == "auto" else language,
            num_speakers=n_spk,
            hotwords=parse_terms(hotwords),
            replacements=parse_replacements(replacements),
            subject=subject.strip() or None,
        )

    def remember_subject(opts: dict) -> None:
        if opts["subject"]:
            # 과목을 정했으면 이번에 쓴 용어를 그 과목에 저장해서 다음 녹음에 다시 채워준다
            db.save_subject(opts["subject"], opts["hotwords"], opts["replacements"])

    @app.post("/api/recordings", status_code=201)
    async def upload(
        file: UploadFile = File(...),
        title: str = Form(""),
        language: str = Form("auto"),
        num_speakers: str = Form(""),
        hotwords: str = Form(""),
        replacements: str = Form(""),
        subject: str = Form(""),
    ) -> dict:
        opts = parse_options(language, num_speakers, hotwords, replacements, subject)
        file_name = "original" + _safe_suffix(file.filename)
        rec = db.create_recording(
            title=title.strip() or Path(file.filename or "녹음").stem, file_name=file_name, **opts
        )
        remember_subject(opts)

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
        if rec["status"] in ("queued", "processing", "recording"):
            raise HTTPException(409, "이미 처리 중이에요.")
        worker.enqueue(rec_id)
        return with_label(db.get_recording(rec_id))

    @app.delete("/api/recordings/{rec_id}", status_code=204)
    def delete(rec_id: str) -> None:
        get_or_404(rec_id)
        awake.release(rec_id)
        db.delete_recording(rec_id)
        shutil.rmtree(recordings_dir / rec_id, ignore_errors=True)

    @app.get("/api/recordings/{rec_id}/audio")
    def audio(rec_id: str) -> FileResponse:
        rec = get_or_404(rec_id)
        path = recordings_dir / rec_id / rec["file_name"]
        if not path.exists():
            raise HTTPException(404, "녹음 파일이 없어요.")
        return FileResponse(path)

    # ---- 검색 / 내보내기 ----

    @app.get("/api/search")
    def search(q: str = "") -> dict:
        q = q.strip()[:100]
        hits = db.search(q) if q else []
        return {"query": q, "count": len(hits), "results": hits}

    @app.get("/api/recordings/{rec_id}/export")
    def export_file(rec_id: str, format: str = "txt", download: bool = True) -> Response:
        rec = get_or_404(rec_id)
        if format not in export.FORMATS:
            raise HTTPException(400, "txt, md, srt 중에서 골라주세요.")
        content_type, ext = export.FORMATS[format]
        body = export.render(format, rec, db.get_transcript(rec_id))
        headers = {}
        if download:
            safe = re.sub(r'[\\/:*?"<>|]+', " ", rec["title"]).strip() or "녹음"
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(safe + ext)}"
        return Response(body, media_type=content_type, headers=headers)

    # ---- 결과 편집 ----

    def utterance_or_404(utt_id: int) -> dict:
        utt = db.get_utterance(utt_id)
        if utt is None:
            raise HTTPException(404, "문단을 찾을 수 없어요.")
        return utt

    @app.patch("/api/recordings/{rec_id}")
    def rename_recording(rec_id: str, body: RecordingPatch) -> dict:
        get_or_404(rec_id)
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "제목을 입력해 주세요.")
        db.update_recording(rec_id, title=title[:120])
        return with_label(db.get_recording(rec_id))

    @app.patch("/api/recordings/{rec_id}/speakers/{idx}")
    def rename_speaker(rec_id: str, idx: int, body: SpeakerPatch) -> dict:
        get_or_404(rec_id)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "이름을 입력해 주세요.")
        db.rename_speaker(rec_id, idx, name[:40])
        return {"idx": idx, "name": name[:40]}

    @app.post("/api/recordings/{rec_id}/speakers/{idx}/merge")
    def merge_speaker(rec_id: str, idx: int, body: SpeakerMerge) -> dict:
        get_or_404(rec_id)
        known = {s["idx"] for s in db.get_transcript(rec_id)["speakers"]}
        if idx not in known or body.into not in known or idx == body.into:
            raise HTTPException(400, "합칠 화자를 다시 골라주세요.")
        db.merge_speaker(rec_id, idx, body.into)
        return db.get_transcript(rec_id)

    @app.post("/api/recordings/{rec_id}/speakers", status_code=201)
    def add_speaker(rec_id: str) -> dict:
        get_or_404(rec_id)
        return db.add_speaker(rec_id)

    @app.patch("/api/utterances/{utt_id}")
    def edit_utterance(utt_id: int, body: UtterancePatch) -> dict:
        utt = utterance_or_404(utt_id)
        if utt["busy"]:
            raise HTTPException(409, "다시 받아쓰는 중이에요. 끝난 뒤에 고쳐주세요.")
        fields: dict = {}
        if body.text is not None:
            text = body.text.strip()
            if not text:
                raise HTTPException(400, "내용이 비어 있어요. 지우려면 '문단 삭제'를 눌러주세요.")
            if text != utt["text"]:
                # 직접 고친 문단은 단어별 시간이 더 이상 맞지 않으므로 비운다 (문단 시간은 유지)
                fields.update(text=text, words=[])
        if body.speaker is not None:
            known = {s["idx"] for s in db.get_transcript(utt["recording_id"])["speakers"]}
            if body.speaker not in known:
                raise HTTPException(400, "없는 화자예요.")
            fields["speaker"] = body.speaker
        if fields:
            db.update_utterance(utt_id, **fields)
        return db.get_utterance(utt_id)

    @app.delete("/api/utterances/{utt_id}", status_code=204)
    def delete_utterance(utt_id: int) -> None:
        utterance_or_404(utt_id)
        db.delete_utterance(utt_id)

    @app.post("/api/utterances/{utt_id}/retranscribe", status_code=202)
    def retranscribe(utt_id: int, body: RetranscribeRequest) -> dict:
        utt = utterance_or_404(utt_id)
        if body.language not in ("auto", "ko", "en"):
            raise HTTPException(400, "언어는 자동/한국어/영어 중에서 골라주세요.")
        if utt["busy"]:
            raise HTTPException(409, "이미 다시 받아쓰는 중이에요.")
        worker.enqueue_retranscribe(utt_id, None if body.language == "auto" else body.language)
        return db.get_utterance(utt_id)

    # ---- 브라우저 녹음 ----

    @app.post("/api/live", status_code=201)
    def live_start(title: str = Form(""), container: str = Form("webm")) -> dict:
        # 형식은 시작할 때 정해 둔다 → 브라우저가 꺼진 녹음도 나중에 합칠 수 있다
        file_name = "original" + live.CONTAINERS.get(container, ".webm")
        rec = db.create_recording(title=title.strip() or "새 녹음", file_name=file_name, source="record")
        db.update_recording(rec["id"], status="recording", stage="recording")
        (recordings_dir / rec["id"]).mkdir(parents=True, exist_ok=True)
        awake.hold(rec["id"])
        return with_label(db.get_recording(rec["id"]))

    @app.put("/api/live/{rec_id}/chunks/{seq}", status_code=204)
    async def live_chunk(rec_id: str, seq: int, request: Request) -> None:
        rec = get_or_404(rec_id)
        if rec["status"] != "recording":
            raise HTTPException(409, "이미 끝난 녹음이에요.")
        if not 0 <= seq < 1_000_000:
            raise HTTPException(400, "잘못된 조각 번호예요.")
        data = await request.body()
        if not data or len(data) > live.MAX_CHUNK_BYTES:
            raise HTTPException(400, "녹음 조각이 비었거나 너무 커요.")
        try:
            live.save_chunk(recordings_dir / rec_id, seq, data)
        except OSError:
            raise HTTPException(507, "저장 공간이 부족해서 녹음을 저장하지 못했어요.")
        awake.hold(rec_id)  # 끊겼다가 다시 이어진 경우

    @app.post("/api/live/{rec_id}/finish")
    def live_finish(
        rec_id: str,
        title: str = Form(""),
        language: str = Form("auto"),
        num_speakers: str = Form(""),
        hotwords: str = Form(""),
        replacements: str = Form(""),
        subject: str = Form(""),
    ) -> dict:
        rec = get_or_404(rec_id)
        if rec["status"] != "recording":
            raise HTTPException(409, "이미 끝난 녹음이에요.")
        opts = parse_options(language, num_speakers, hotwords, replacements, subject)
        awake.release(rec_id)
        try:
            file_name = live.assemble(recordings_dir / rec_id, Path(rec["file_name"]).suffix)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.update_recording(rec_id, file_name=file_name, title=title.strip() or rec["title"], **opts)
        remember_subject(opts)
        worker.enqueue(rec_id)
        return with_label(db.get_recording(rec_id))

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
