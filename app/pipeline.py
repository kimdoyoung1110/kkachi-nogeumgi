"""녹음 하나를 끝까지 처리한다: 화자 구분 → 전사 → 단어 정렬 → 화자별 문단."""

from __future__ import annotations

from typing import Optional

import numpy as np

from app.diarize import Diarizer
from app.merge import Utterance, assign_speakers, build_utterances
from app.stt import ProgressFn, Transcriber


class Pipeline:
    def __init__(self):
        self.transcriber = Transcriber()
        self.diarizer = Diarizer()

    def warmup(self) -> None:
        """모델을 미리 메모리에 올려둔다."""
        self.transcriber.asr, self.transcriber.aligner, self.diarizer.model

    def process(
        self,
        audio: np.ndarray,
        language: Optional[str] = None,
        hotwords: Optional[list[str]] = None,
        num_speakers: Optional[int] = None,
        progress: Optional[ProgressFn] = None,
    ) -> list[Utterance]:
        if progress:
            progress("diarize", 0, 1)
        activity = self.diarizer.run(audio)
        if progress:
            progress("diarize", 1, 1)

        segments = self.transcriber.transcribe(audio, language, hotwords, progress)
        words = assign_speakers(segments, activity, num_speakers)
        return build_utterances(words)
