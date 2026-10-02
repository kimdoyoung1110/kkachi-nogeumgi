"""녹음 하나를 끝까지 처리한다.

화자 구분 → (자동 모드) 조각별 언어 감지 → 같은 언어끼리 30초 이내 구간 → 언어 고정 전사
→ 단어 정렬 → 화자별 문단
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from app import config, segmenter
from app.diarize import Diarizer
from app.langid import LanguageIdentifier, fill_unknown
from app.merge import Utterance, assign_speakers, build_utterances
from app.stt import ProgressFn, Segment, Transcriber


class Pipeline:
    def __init__(self):
        self.transcriber = Transcriber()
        self.diarizer = Diarizer()
        self.langid = LanguageIdentifier()

    def warmup(self) -> None:
        """모델을 미리 메모리에 올려둔다."""
        self.transcriber.asr, self.transcriber.aligner, self.diarizer.model, self.langid.model

    def process(
        self,
        audio: np.ndarray,
        language: Optional[str] = None,
        hotwords: Optional[list[str]] = None,
        num_speakers: Optional[int] = None,
        replacements: Optional[dict[str, str]] = None,
        progress: Optional[ProgressFn] = None,
    ) -> list[Utterance]:
        """language: None 이면 한·영 혼합 자동 (조각마다 언어 감지)."""
        if progress:
            progress("diarize", 0, 1)
        activity = self.diarizer.run(audio)
        if progress:
            progress("diarize", 1, 1)

        turns = segmenter.turns_from_probs(activity.probs, activity.frame_seconds)
        spans, span_langs = self._plan(audio, turns, language, progress)

        segments = self.transcriber.transcribe(
            audio, language, hotwords, progress,
            spans=spans, replacements=replacements, span_languages=span_langs,
        )
        words = assign_speakers(segments, activity, num_speakers)
        return build_utterances(words)

    def _plan(self, audio, turns, language, progress) -> tuple[list[segmenter.Span], list[Optional[str]]]:
        """전사할 구간과 구간별 언어를 정한다.

        언어를 지정했으면 화자 교체 지점을 우선해 30초 이내로 자르기만 한다.
        자동이면 짧은 조각마다 언어를 판정하고, 같은 언어끼리 묶은 뒤 30초 이내로 자른다.
        → 한 구간 안에는 한 언어만 들어가므로 Qwen이 다른 언어를 '번역'해 버리지 않는다.
        """
        sr, max_s = config.SAMPLE_RATE, self.transcriber.chunk_seconds
        if language is not None:
            spans = segmenter.split_by_turns(audio, turns, sr, max_s)
            return spans, [language] * len(spans)

        pieces = segmenter.pieces_from_turns(audio, turns, sr)
        if not pieces:
            spans = segmenter.split_by_turns(audio, turns, sr, max_s)
            return spans, [None] * len(spans)
        if progress:
            progress("langid", 0, len(pieces))
        raw = []
        for i, piece in enumerate(pieces):
            raw.append(self.langid.identify(audio[piece.span.start:piece.span.end]))
            if progress and ((i + 1) % 20 == 0 or i + 1 == len(pieces)):
                progress("langid", i + 1, len(pieces))
        labels = fill_unknown(raw)

        spans, langs = [], []
        for a, b, lang in segmenter.language_regions(pieces, labels, len(audio)):
            for sp in segmenter.split_region_by_turns(audio, turns, a, b, sr, max_s):
                spans.append(sp)
                langs.append(lang)
        return spans, langs

    def retranscribe(
        self,
        audio: np.ndarray,
        start: float,
        end: float,
        language: Optional[str],
        hotwords: Optional[list[str]] = None,
        replacements: Optional[dict[str, str]] = None,
    ) -> list[Segment]:
        """문단 하나를 지정한 언어로 다시 전사한다 (결과 화면의 '다른 언어로 다시 전사')."""
        sr = config.SAMPLE_RATE
        a, b = max(0, int(start * sr) - int(0.2 * sr)), min(len(audio), int(end * sr) + int(0.2 * sr))
        spans = [segmenter.Span(a + s.start, a + s.end)
                 for s in segmenter.split(audio[a:b], sr, self.transcriber.chunk_seconds)]
        return self.transcriber.transcribe(
            audio, language, hotwords, spans=spans, replacements=replacements
        )
