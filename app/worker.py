"""백그라운드 작업 처리기. 녹음을 한 번에 하나씩 처리한다 (GPU 모델을 동시에 돌리면 오히려 느림).

- 처리 중에는 맥이 잠들지 않게 caffeinate 를 건다.
- 앱이 처리 도중 꺼졌다 다시 켜지면, 멈춘 작업은 '실패(중단됨)'로 표시하고 대기 중이던 작업은 다시 줄 세운다.
- 원본 녹음 파일은 실패해도 절대 지우지 않는다.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app import audio as audio_io
from app.db import Database

log = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "처리 중에 앱이 꺼져서 멈췄어요. [다시 시도]를 눌러주세요."

# 단계별 전체 진행률 비중 (합 1.0). 10분 녹음 실측 시간 비율 기준.
STAGE_WEIGHTS = {
    "decode": 0.02,
    "diarize": 0.02,
    "langid": 0.04,
    "transcribe": 0.65,
    "align": 0.27,
}
STAGE_ORDER = list(STAGE_WEIGHTS)
STAGE_LABELS = {
    "recording": "녹음 중",
    "queued": "대기 중",
    "decode": "파일 읽는 중",
    "diarize": "화자 구분 중",
    "langid": "언어 확인 중",
    "transcribe": "받아쓰는 중",
    "align": "시간 맞추는 중",
    "done": "완료",
    "failed": "실패",
}


def overall_progress(stage: str, done: int, total: int) -> float:
    before = sum(STAGE_WEIGHTS[s] for s in STAGE_ORDER[: STAGE_ORDER.index(stage)])
    frac = done / total if total else 1.0
    return round(min(1.0, before + STAGE_WEIGHTS[stage] * frac), 4)


def friendly_error(exc: BaseException) -> str:
    """비개발자용 오류 문구."""
    if isinstance(exc, audio_io.AudioDecodeError):
        return "이 파일은 소리를 읽을 수 없어요. 다른 형식(m4a, mp3, wav)으로 다시 시도해 주세요."
    if isinstance(exc, MemoryError):
        return "메모리가 부족해요. 다른 앱을 닫고 [다시 시도]를 눌러주세요."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "저장 공간이 부족해요. 공간을 확보한 뒤 [다시 시도]를 눌러주세요."
    return "처리 중 문제가 생겼어요. [다시 시도]를 눌러보고, 계속되면 로그를 보내주세요."


class Worker:
    def __init__(self, db: Database, recordings_dir: Path, pipeline_factory: Callable[[], object]):
        self.db = db
        self.recordings_dir = recordings_dir
        self._pipeline_factory = pipeline_factory
        self._pipeline = None
        # 작업: ("process", rec_id) 또는 ("retranscribe", utt_id, language). None 은 종료 신호
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._last_write = 0.0
        # 같은 녹음의 문단을 연달아 다시 받아쓸 때 매번 디코딩하지 않도록 하나만 들고 있는다
        self._audio_cache: tuple[Optional[str], object] = (None, None)

    @property
    def pipeline(self):
        if self._pipeline is None:
            self._pipeline = self._pipeline_factory()
        return self._pipeline

    # ---- 생명주기 ----

    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._run, name="kkachi-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)

    def recover(self) -> None:
        """지난번 실행에서 처리 중이던 건 실패로, 대기 중이던 건 다시 줄 세운다."""
        for rec_id in self.db.ids_with_status("processing"):
            self.db.update_recording(rec_id, status="failed", stage="failed", error=INTERRUPTED_MESSAGE)
        for rec_id in self.db.ids_with_status("queued"):
            self._queue.put(("process", rec_id))
        self.db.clear_busy()

    def enqueue(self, rec_id: str) -> None:
        self.db.update_recording(
            rec_id, status="queued", stage="queued", progress=0.0, error=None, processing_secs=None
        )
        self._queue.put(("process", rec_id))

    def enqueue_retranscribe(self, utt_id: int, language: Optional[str]) -> None:
        self.db.update_utterance(utt_id, busy=1)
        self._queue.put(("retranscribe", utt_id, language))

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """테스트용: 큐가 빌 때까지 기다린다."""
        end = time.time() + timeout
        while time.time() < end:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False

    # ---- 처리 ----

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                if task[0] == "process":
                    self._process(task[1])
                elif task[0] == "retranscribe":
                    self._retranscribe(task[1], task[2])
            finally:
                self._queue.task_done()

    def _process(self, rec_id: str) -> None:
        rec = self.db.get_recording(rec_id)
        if rec is None or rec["status"] != "queued":
            return  # 그 사이 삭제됐거나 이미 처리됨

        started = time.time()
        self.db.update_recording(rec_id, status="processing", stage="decode", progress=0.0, error=None)
        caffeinate = _start_caffeinate()
        try:
            audio = self._load_audio(rec)
            self.db.update_recording(rec_id, duration=audio_io.duration(audio))
            self._report(rec_id, "decode", 1, 1, force=True)

            utterances = self.pipeline.process(
                audio,
                language=rec["language"],
                hotwords=rec["hotwords"] or None,
                num_speakers=rec["num_speakers"],
                replacements=rec["replacements"] or None,
                progress=lambda stage, done, total: self._report(rec_id, stage, done, total),
            )
            if self.db.get_recording(rec_id) is None:
                return  # 처리 중에 삭제됨
            self.db.save_transcript(rec_id, utterances)
            self.db.update_recording(
                rec_id, status="done", stage="done", progress=1.0,
                processing_secs=round(time.time() - started, 1),
            )
        except Exception as exc:
            log.exception("녹음 처리 실패: %s", rec_id)
            if self.db.get_recording(rec_id) is not None:
                self.db.update_recording(rec_id, status="failed", stage="failed", error=friendly_error(exc))
        finally:
            if caffeinate:
                caffeinate.terminate()

    def _load_audio(self, rec: dict):
        if self._audio_cache[0] == rec["id"]:
            return self._audio_cache[1]
        self._audio_cache = (None, None)  # 큰 배열을 두 개 들고 있지 않게 먼저 비운다
        audio = audio_io.decode(self.recordings_dir / rec["id"] / rec["file_name"])
        self._audio_cache = (rec["id"], audio)
        return audio

    def _retranscribe(self, utt_id: int, language: Optional[str]) -> None:
        """문단 하나를 지정한 언어로 다시 받아쓴다. 실패하면 원래 내용을 그대로 둔다."""
        from app.merge import SpeakerWord, utterance_language
        from app.stt import word_language

        utt = self.db.get_utterance(utt_id)
        if utt is None:
            return
        rec = self.db.get_recording(utt["recording_id"])
        try:
            audio = self._load_audio(rec)
            segments = self.pipeline.retranscribe(
                audio, utt["start"], utt["end"], language,
                hotwords=rec["hotwords"] or None, replacements=rec["replacements"] or None,
            )
            text = " ".join(seg.text for seg in segments).strip()
            if not text:
                self.db.update_utterance(utt_id, busy=0)
                return
            words = [w for seg in segments for w in seg.words]
            lang = language or utterance_language([
                SpeakerWord(w.text, w.start, w.end, utt["speaker"], word_language(w.text, seg.language))
                for seg in segments for w in seg.words
            ] or [SpeakerWord(text, 0, 0, 0, segments[0].language)])
            self.db.update_utterance(
                utt_id, text=text, language=lang, busy=0,
                words=[[w.text, round(w.start, 3), round(w.end, 3)] for w in words],
            )
        except Exception:
            log.exception("문단 다시 받아쓰기 실패: %s", utt_id)
            if self.db.get_utterance(utt_id):
                self.db.update_utterance(utt_id, busy=0)

    def _report(self, rec_id: str, stage: str, done: int, total: int, force: bool = False) -> None:
        # DB 쓰기는 0.5초에 한 번만 (단계가 끝날 때는 항상)
        now = time.time()
        if not force and done != total and now - self._last_write < 0.5:
            return
        self._last_write = now
        if stage in STAGE_WEIGHTS:
            self.db.update_recording(rec_id, stage=stage, progress=overall_progress(stage, done, total))


def _start_caffeinate() -> Optional[subprocess.Popen]:
    try:
        # -i: 처리 중 잠자기 방지 (화면은 꺼져도 됨)
        # -w: 앱이 강제 종료돼도 caffeinate 가 같이 끝나도록 우리 프로세스를 지켜본다
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except OSError:
        return None
