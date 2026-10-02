"""Qwen3-ASR 전사 + Qwen3-ForcedAligner 단어 타임스탬프.

흐름: 오디오 → 구간 분할(segmenter) → 구간들을 배치로 전사 → 구간마다 언어 파싱
      → 구간마다 단어 정렬 → Segment 목록
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from app import config, segmenter

log = logging.getLogger(__name__)

# 화면/DB에서 쓰는 언어 코드 ↔ Qwen이 쓰는 언어 이름
LANG_TO_QWEN = {"ko": "Korean", "en": "English"}
QWEN_TO_LANG = {v: k for k, v in LANG_TO_QWEN.items()}

# 30초 구간 하나에서 나올 수 있는 토큰 상한. 빠른 한국어 강의도 보통 300 이하.
TOKENS_PER_CHUNK = 448

_LANG_PREFIX = re.compile(r"^\s*language\s+([A-Za-z]+)\s*<asr_text>", re.DOTALL)

ProgressFn = Callable[[str, int, int], None]


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Segment:
    start: float
    end: float
    language: str  # "ko", "en", 그 외는 소문자 언어 이름
    text: str
    words: list[Word] = field(default_factory=list)


def parse_language(raw: str, forced: Optional[str]) -> tuple[Optional[str], str]:
    """'language Korean<asr_text>본문' → ("ko", "본문"). 말소리가 없으면 언어가 None."""
    m = _LANG_PREFIX.match(raw)
    if m:
        name, text = m.group(1), raw[m.end():]
    else:
        name, text = (LANG_TO_QWEN.get(forced) if forced else None), raw
    text = text.strip()
    if not name or name.lower() == "none":
        return None, text
    return QWEN_TO_LANG.get(name, name.lower()), text


def looks_repetitive(text: str, min_unit: int = 2, max_unit: int = 20, times: int = 6) -> bool:
    """같은 문구가 연달아 여러 번 반복되면 모델이 루프에 빠진 것."""
    pattern = re.compile(r"(.{%d,%d}?)\1{%d,}" % (min_unit, max_unit, times - 1), re.DOTALL)
    return bool(pattern.search(text))


def attach_words(text: str, tokens: list[tuple[str, float, float]], offset: float) -> list[Word]:
    """정렬기 토큰(구두점 제거됨)을 원문 띄어쓰기 단위 단어(구두점 포함)에 시간으로 매핑한다."""
    # 1) 토큰 → 원문 글자 위치
    spans: list[tuple[int, int, float, float]] = []
    cursor = 0
    lower = text.lower()
    for tok, s, e in tokens:
        idx = lower.find(tok.lower(), cursor)
        if idx < 0:
            continue
        spans.append((idx, idx + len(tok), s, e))
        cursor = idx + len(tok)

    # 2) 원문 단어마다 겹치는 토큰들의 시간 범위
    words: list[Word] = []
    for m in re.finditer(r"\S+", text):
        times = [(s, e) for a, b, s, e in spans if a < m.end() and b > m.start()]
        if times:
            words.append(Word(m.group(), offset + min(t[0] for t in times), offset + max(t[1] for t in times)))
        else:
            words.append(Word(m.group(), float("nan"), float("nan")))

    # 3) 시간이 없는 단어는 앞뒤 단어 사이로 채운다
    for i, w in enumerate(words):
        if np.isnan(w.start):
            prev_end = next((x.end for x in reversed(words[:i]) if not np.isnan(x.end)), offset)
            next_start = next((x.start for x in words[i + 1:] if not np.isnan(x.start)), prev_end)
            w.start, w.end = prev_end, max(prev_end, next_start)
    return words


class Transcriber:
    def __init__(
        self,
        asr_model: str = config.ASR_MODEL,
        aligner_model: str = config.ALIGNER_MODEL,
        batch_size: int = config.ASR_BATCH_SIZE,
        chunk_seconds: float = config.ASR_CHUNK_SECONDS,
    ):
        self.asr_model_name = asr_model
        self.aligner_model_name = aligner_model
        self.batch_size = batch_size
        self.chunk_seconds = chunk_seconds
        self._asr = None
        self._aligner = None

    # 모델은 처음 쓸 때 한 번만 불러온다
    @property
    def asr(self):
        if self._asr is None:
            from mlx_audio.stt.utils import load_model

            self._asr = load_model(self.asr_model_name)
        return self._asr

    @property
    def aligner(self):
        if self._aligner is None:
            from mlx_audio.stt.utils import load_model

            self._aligner = load_model(self.aligner_model_name)
        return self._aligner

    def transcribe(
        self,
        audio: np.ndarray,
        language: Optional[str] = None,
        hotwords: Optional[list[str]] = None,
        progress: Optional[ProgressFn] = None,
        align: bool = True,
    ) -> list[Segment]:
        """language: None(자동, 구간마다 감지) / "ko" / "en"."""
        sr = config.SAMPLE_RATE
        spans = [s for s in segmenter.split(audio, sr, self.chunk_seconds)
                 if not segmenter.is_silent(audio[s.start:s.end])]
        chunks = [(audio[s.start:s.end], s.start / sr) for s in spans]

        raw_texts = self._transcribe_chunks(chunks, language, hotwords, progress)

        segments: list[Segment] = []
        for (chunk, offset), raw in zip(chunks, raw_texts):
            lang, text = parse_language(raw, language)
            if not text or lang is None:
                continue
            segments.append(Segment(offset, offset + len(chunk) / sr, lang, text))

        if align:
            self._align(segments, chunks, progress)
        return segments

    def _transcribe_chunks(self, chunks, language, hotwords, progress) -> list[str]:
        from mlx_audio.lm.sample_utils import make_logits_processors, make_sampler
        from mlx_audio.stt.utils import merge_hotwords

        model = self.asr
        sampler = make_sampler(0.0, 1.0, 0.0, min_tokens_to_keep=1, top_k=0)
        qwen_lang = LANG_TO_QWEN.get(language) if language else None
        system_prompt = merge_hotwords(None, hotwords)

        def run(group, logits_processors=None):
            # mlx-audio 0.5.7 내부 함수. 구간 단위 언어 감지와 배치 처리를 우리가 직접 제어하려고 사용.
            texts, *_ = model._generate_chunks_batched(
                group,
                max_tokens=TOKENS_PER_CHUNK * len(group),
                sampler=sampler,
                logits_processors=logits_processors,
                language=qwen_lang,
                system_prompt=system_prompt,
                batch_size=len(group),
                verbose=False,
            )
            return texts

        out: list[str] = []
        if progress:
            progress("transcribe", 0, len(chunks))
        for b0 in range(0, len(chunks), self.batch_size):
            out.extend(run(chunks[b0:b0 + self.batch_size]))
            if progress:
                progress("transcribe", min(b0 + self.batch_size, len(chunks)), len(chunks))

        # 반복 루프에 빠진 구간은 반복 억제를 켜고 하나씩 다시
        retry = make_logits_processors(repetition_penalty=1.15, repetition_context_size=100)
        for i, text in enumerate(out):
            if looks_repetitive(text):
                out[i] = run([chunks[i]], retry)[0]
        return out

    def _align(self, segments: list[Segment], chunks, progress) -> None:
        by_offset = {offset: chunk for chunk, offset in chunks}
        if progress:
            progress("align", 0, len(segments))
        for i, seg in enumerate(segments):
            chunk = by_offset[seg.start]
            qwen_lang = LANG_TO_QWEN.get(seg.language, "English")
            try:
                result = self.aligner.generate(chunk, text=seg.text, language=qwen_lang)
            except Exception:
                # 정렬이 실패해도 전사 결과는 살린다 (단어 시간은 구간 시간으로 대체)
                log.exception("단어 정렬 실패: %.1fs 구간", seg.start)
                continue
            tokens = [(it.text, it.start_time, it.end_time) for it in result]
            seg.words = attach_words(seg.text, tokens, seg.start)
            if seg.words:
                seg.start = seg.words[0].start
                seg.end = seg.words[-1].end
            if progress:
                progress("align", i + 1, len(segments))
