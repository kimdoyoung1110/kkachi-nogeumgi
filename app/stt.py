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


_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_LATIN = re.compile(r"[A-Za-z]")


def word_language(text: str, fallback: str) -> str:
    """단어 하나의 언어를 글자로 판단한다. 한글이 섞이면 ko (collision이라고 → ko), 영문만 있으면 en."""
    if _HANGUL.search(text):
        return "ko"
    if _LATIN.search(text):
        return "en"
    return fallback


def apply_replacements(text: str, replacements: Optional[dict[str, str]]) -> str:
    """용어 바꾸기 사전 적용. 긴 표현부터 바꿔서 '세프리 체인잉'이 '체인잉'보다 먼저 처리되게 한다."""
    if not replacements:
        return text
    for src in sorted(replacements, key=len, reverse=True):
        if src:
            text = text.replace(src, replacements[src])
    return text


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
        spans: Optional[list[segmenter.Span]] = None,
        replacements: Optional[dict[str, str]] = None,
        span_languages: Optional[list[str]] = None,
    ) -> list[Segment]:
        """language: None(자동, 구간마다 감지) / "ko" / "en".

        spans 를 주면 그 구간대로 전사한다 (보통 화자 턴 기준). 없으면 30초 단위로 자른다.
        replacements 는 전사 결과에 적용할 용어 바꾸기 사전 (예: {"컬리전": "collision"}).
        span_languages 를 주면 구간마다 그 언어로 고정해서 전사한다 (혼합 모드). language 보다 우선.
        """
        sr = config.SAMPLE_RATE
        if spans is None:
            spans = segmenter.split(audio, sr, self.chunk_seconds)
        if span_languages is None:
            span_languages = [language] * len(spans)
        keep = [i for i, s in enumerate(spans) if not segmenter.is_silent(audio[s.start:s.end])]
        chunks = [(audio[spans[i].start:spans[i].end], spans[i].start / sr) for i in keep]
        langs = [span_languages[i] for i in keep]

        raw_texts = self._transcribe_chunks(chunks, langs, hotwords, progress)

        segments: list[Segment] = []
        for (chunk, offset), raw, forced in zip(chunks, raw_texts, langs):
            lang, text = parse_language(raw, forced)
            if not text or lang is None:
                continue
            segments.append(Segment(offset, offset + len(chunk) / sr, lang, apply_replacements(text, replacements)))

        if align:
            self._align(segments, chunks, progress)
        return segments

    def _transcribe_chunks(self, chunks, languages, hotwords, progress) -> list[str]:
        """languages: 구간마다 None(자동) / "ko" / "en". 같은 언어끼리 배치로 묶는다."""
        from mlx_audio.lm.sample_utils import make_logits_processors, make_sampler
        from mlx_audio.stt.utils import merge_hotwords

        model = self.asr
        sampler = make_sampler(0.0, 1.0, 0.0, min_tokens_to_keep=1, top_k=0)
        system_prompt = merge_hotwords(None, hotwords)

        def run(group, lang, logits_processors=None):
            # mlx-audio 0.5.7 내부 함수. 구간 단위 언어 감지와 배치 처리를 우리가 직접 제어하려고 사용.
            texts, *_ = model._generate_chunks_batched(
                group,
                max_tokens=TOKENS_PER_CHUNK * len(group),
                sampler=sampler,
                logits_processors=logits_processors,
                language=LANG_TO_QWEN.get(lang) if lang else None,
                system_prompt=system_prompt,
                batch_size=len(group),
                verbose=False,
            )
            return texts

        # 배치 안에서는 가장 긴 구간 길이에 맞춰 패딩되므로 길이순으로 묶어서 낭비를 줄인다
        order = sorted(range(len(chunks)), key=lambda i: (str(languages[i]), len(chunks[i][0])))
        batches: list[list[int]] = []
        for i in order:
            if batches and len(batches[-1]) < self.batch_size and languages[batches[-1][0]] == languages[i]:
                batches[-1].append(i)
            else:
                batches.append([i])

        out: list[str] = [""] * len(chunks)
        done = 0
        if progress:
            progress("transcribe", 0, len(chunks))
        for idx in batches:
            for i, text in zip(idx, run([chunks[i] for i in idx], languages[idx[0]])):
                out[i] = text
            done += len(idx)
            if progress:
                progress("transcribe", done, len(chunks))

        # 반복 루프에 빠진 구간은 반복 억제를 켜고 하나씩 다시
        retry = make_logits_processors(repetition_penalty=1.15, repetition_context_size=100)
        for i, text in enumerate(out):
            if looks_repetitive(text):
                out[i] = run([chunks[i]], languages[i], retry)[0]
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
