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

from app import config, export, live, slides, system, terms, voicememos
from app.db import Database
from app.worker import STAGE_LABELS, Worker

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UPLOAD_CHUNK = 1024 * 1024


class RecordingPatch(BaseModel):
    title: Optional[str] = None
    subject: Optional[str] = None   # 분류. 빈 문자열이면 '분류 없음'


class SpeakerPatch(BaseModel):
    name: str


class MarkRequest(BaseModel):
    t: float


class NoteRequest(BaseModel):
    t: float
    text: str


class NotePatch(BaseModel):
    text: str


class VoiceMemoImport(BaseModel):
    keys: list[str]
    subject: str = ""
    language: str = "auto"


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
    app.state.voicememos_root = voicememos.ROOT   # 테스트에서 바꿈

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

    @app.post("/api/app/client")
    async def client_info(request: Request) -> dict:
        """화면이 켜질 때 보내는 환경 정보 (녹음 가능 여부 등). 문제 신고용 로그에 남긴다."""
        try:
            info = await request.json()
        except Exception:
            info = {}
        log.info("화면 환경: %s", {k: info.get(k) for k in ("inApp", "mediaRecorder", "mime", "getUserMedia", "ua")})
        return {"ok": True}

    @app.get("/api/app/power")
    def power() -> dict:
        return system.power_status()

    @app.get("/api/app/gift")
    def gift() -> dict:
        return system.load_gift(data_dir)

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
        result = system.apply_update(data_dir)
        if not result["ok"]:
            raise HTTPException(500, result["message"])
        log.info("업데이트 완료, 다시 시작: %s", result["version"])
        system.restart_soon()
        return {**result, "restarting": True}

    @app.get("/api/app/whatsnew")
    def whatsnew() -> dict:
        unseen = system.unseen_notes(data_dir)
        return {"notes": system.load_notes(), "unseen": [n["id"] for n in unseen]}

    @app.post("/api/app/whatsnew/seen")
    def whatsnew_seen() -> dict:
        system.mark_notes_seen(data_dir)
        return {"ok": True}

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
        transcript = db.get_transcript(rec_id)
        return {**with_label(rec), **transcript,
                "slides": slides.info(recordings_dir / rec_id, transcript["utterances"])}

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
        # 녹음에 붙여 둔 강의자료(슬라이드)에서도 찾는다
        slide_hits = []
        if q:
            for rec in db.list_recordings():
                for h in slides.search(recordings_dir / rec["id"], q):
                    slide_hits.append({**h, "recording_id": rec["id"], "title": rec["title"],
                                       "created_at": rec["created_at"]})
        return {"query": q, "count": len(hits), "results": hits, "slides": slide_hits[:100]}

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
    def edit_recording(rec_id: str, body: RecordingPatch) -> dict:
        rec = get_or_404(rec_id)
        fields: dict = {}
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(400, "제목을 입력해 주세요.")
            fields["title"] = title[:120]
        if body.subject is not None:
            subject = body.subject.strip()[:40] or None
            fields["subject"] = subject
            if subject and subject not in {x["name"] for x in db.list_subjects()}:
                # 새 분류: 이 녹음의 용어를 그 분류에 저장해 둔다 (다음 업로드에 자동으로 채워짐)
                db.save_subject(subject, rec["hotwords"], rec["replacements"])
        if fields:
            db.update_recording(rec_id, **fields)
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

    # ---- 중요 표시 ----

    @app.post("/api/recordings/{rec_id}/marks", status_code=201)
    def add_mark(rec_id: str, body: MarkRequest) -> dict:
        get_or_404(rec_id)
        if body.t < 0:
            raise HTTPException(400, "잘못된 시간이에요.")
        db.add_mark(rec_id, round(body.t, 2))
        return {"marks": db.list_marks(rec_id)}

    @app.delete("/api/recordings/{rec_id}/marks")
    def delete_marks(rec_id: str, start: float, end: float) -> dict:
        get_or_404(rec_id)
        db.delete_marks(rec_id, start, end)
        return {"marks": db.list_marks(rec_id)}

    # ---- 녹음 중 메모 ----

    def clean_note(text: str) -> str:
        text = text.strip()
        if not text:
            raise HTTPException(400, "메모가 비었어요.")
        return text[:500]

    @app.post("/api/recordings/{rec_id}/notes", status_code=201)
    def add_note(rec_id: str, body: NoteRequest) -> dict:
        get_or_404(rec_id)
        if body.t < 0:
            raise HTTPException(400, "잘못된 시간이에요.")
        db.add_note(rec_id, round(body.t, 2), clean_note(body.text))
        return {"notes": db.list_notes(rec_id)}

    @app.patch("/api/recordings/{rec_id}/notes/{note_id}")
    def edit_note(rec_id: str, note_id: int, body: NotePatch) -> dict:
        if not db.update_note(rec_id, note_id, clean_note(body.text)):
            raise HTTPException(404, "메모를 찾을 수 없어요.")
        return {"notes": db.list_notes(rec_id)}

    @app.delete("/api/recordings/{rec_id}/notes/{note_id}")
    def delete_note(rec_id: str, note_id: int) -> dict:
        if not db.delete_note(rec_id, note_id):
            raise HTTPException(404, "메모를 찾을 수 없어요.")
        return {"notes": db.list_notes(rec_id)}

    # ---- 녹음에 강의자료(PDF) 붙여 두기 ----

    @app.post("/api/recordings/{rec_id}/slides")
    async def attach_slides(rec_id: str, file: UploadFile = File(...)) -> dict:
        rec = get_or_404(rec_id)
        if Path(file.filename or "").suffix.lower() != ".pdf":
            raise HTTPException(400, "PDF 파일만 붙일 수 있어요. 파워포인트는 'PDF로 내보내기' 한 뒤 붙여주세요.")
        data = await file.read(slides.MAX_BYTES + 1)
        if len(data) > slides.MAX_BYTES:
            raise HTTPException(400, "파일이 너무 커요 (150MB 까지).")
        try:
            meta = slides.save(recordings_dir / rec_id, file.filename or "강의자료.pdf", data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError:
            raise HTTPException(507, "저장 공간이 부족해서 강의자료를 저장하지 못했어요.")

        # 강의자료의 전공 용어를 이 녹음(다시 받아쓰기 때)과 과목의 용어 힌트에 더한다
        found = terms.extract_terms("\n".join(meta["pages"]))
        have = {t.lower() for t in rec["hotwords"]}
        new = [t for t in found if t.lower() not in have]
        if new:
            db.update_recording(rec_id, hotwords=rec["hotwords"] + new)
            if rec["subject"]:
                saved = next((x for x in db.list_subjects() if x["name"] == rec["subject"]), None)
                old = saved["hotwords"] if saved else []
                olds = {t.lower() for t in old}
                db.save_subject(rec["subject"], old + [t for t in new if t.lower() not in olds],
                                saved["replacements"] if saved else rec["replacements"])
        log.info("강의자료 붙임: %s쪽, 용어 %s개 추가", len(meta["pages"]), len(new))
        info = slides.info(recordings_dir / rec_id, db.get_transcript(rec_id)["utterances"])
        return {"slides": info, "terms_added": new}

    @app.get("/api/recordings/{rec_id}/slides.pdf")
    def slides_file(rec_id: str) -> FileResponse:
        get_or_404(rec_id)
        path = recordings_dir / rec_id / slides.PDF_NAME
        if not path.exists():
            raise HTTPException(404, "붙여 둔 강의자료가 없어요.")
        return FileResponse(path, media_type="application/pdf")

    @app.delete("/api/recordings/{rec_id}/slides", status_code=204)
    def detach_slides(rec_id: str) -> None:
        get_or_404(rec_id)
        slides.remove(recordings_dir / rec_id)

    # ---- 강의자료에서 용어 뽑기 ----

    @app.post("/api/terms/extract")
    async def extract_terms(file: UploadFile = File(...)) -> dict:
        data = await file.read(terms.MAX_BYTES + 1)
        if len(data) > terms.MAX_BYTES:
            raise HTTPException(400, "파일이 너무 커요 (80MB 까지).")
        try:
            text = terms.extract_text(file.filename or "", data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if len(text.strip()) < 20:
            raise HTTPException(400, "파일에서 글자를 읽지 못했어요. 스캔한 PDF(사진)는 읽을 수 없어요.")
        found = terms.extract_terms(text)
        log.info("강의자료 용어 뽑기: %s글자 → %s개", len(text), len(found))
        return {"terms": found, "chars": len(text)}

    # ---- 아이폰 음성 메모 가져오기 ----

    @app.get("/api/voicememos")
    def voicememo_list() -> dict:
        try:
            items = voicememos.list_memos(app.state.voicememos_root)
        except voicememos.NoPermission:
            return {"status": "permission", "items": []}
        if items is None:
            return {"status": "missing", "items": []}
        done = db.origins("voicememo:")
        for m in items:
            m["imported"] = f"voicememo:{m['key']}" in done
        return {"status": "ok", "items": items}

    @app.post("/api/voicememos/import", status_code=201)
    def voicememo_import(body: VoiceMemoImport) -> list[dict]:
        if not body.keys:
            raise HTTPException(400, "가져올 음성 메모를 골라주세요.")
        subject = body.subject.strip()
        saved = next((x for x in db.list_subjects() if x["name"] == subject), None) if subject else None
        opts = parse_options(body.language, "", "", "", subject)
        if saved:  # 과목에 저장해 둔 용어 힌트를 그대로 쓴다
            opts.update(hotwords=saved["hotwords"], replacements=saved["replacements"])
        try:
            memos = {m["key"]: m for m in voicememos.list_memos(app.state.voicememos_root, limit=100_000) or []}
        except voicememos.NoPermission:
            raise HTTPException(403, "음성 메모 폴더를 읽을 권한이 없어요.")
        done = db.origins("voicememo:")
        created = []
        for key in body.keys:
            m = memos.get(key)
            if m is None or f"voicememo:{key}" in done:
                continue
            src = voicememos.recordings_dir(app.state.voicememos_root) / m["file"]
            file_name = "original" + _safe_suffix(m["file"])
            rec = db.create_recording(title=m["title"], file_name=file_name, source="voicememo",
                                      created_at=m["date"], origin=f"voicememo:{key}", **opts)
            dest = recordings_dir / rec["id"]
            dest.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest / file_name)
            except OSError:
                db.delete_recording(rec["id"])
                shutil.rmtree(dest, ignore_errors=True)
                raise HTTPException(507, f"‘{m['title']}’을 복사하지 못했어요. 저장 공간을 확인해 주세요.")
            worker.enqueue(rec["id"])
            created.append(with_label(db.get_recording(rec["id"])))
        log.info("음성 메모 가져오기: %s개", len(created))
        return created

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
    async def live_chunk(rec_id: str, seq: int, request: Request, track: Optional[str] = None) -> None:
        rec = get_or_404(rec_id)
        if rec["status"] != "recording":
            raise HTTPException(409, "이미 끝난 녹음이에요.")
        if not 0 <= seq < 1_000_000:
            raise HTTPException(400, "잘못된 조각 번호예요.")
        if track is not None and track not in live.PCM_TRACKS:
            raise HTTPException(400, "잘못된 트랙이에요.")
        data = await request.body()
        if not data or len(data) > live.MAX_CHUNK_BYTES:
            raise HTTPException(400, "녹음 조각이 비었거나 너무 커요.")
        try:
            live.save_chunk(recordings_dir / rec_id, seq, data, track)
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
