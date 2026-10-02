"""전사 결과(단어 + 시간)와 화자 구분 결과를 합쳐 '누가 무슨 말을 했나' 문단을 만든다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.diarize import SpeakerActivity
from app.stt import Segment, Word, word_language

# 문단 안에서 소수 언어가 이 비율 이상이면 'mixed' 로 표시
MIXED_MIN_SHARE = 0.3
# 문단을 끊는 기준: 같은 화자라도 이만큼 길어지면 문장 끝에서 새 문단
PARAGRAPH_MAX_SECONDS = 60.0
# 같은 화자라도 문장 끝에서 이만큼 쉬면 새 문단
PARAGRAPH_PAUSE_SECONDS = 2.0
SENTENCE_END = (".", "?", "!", "。", "？", "！")


@dataclass
class SpeakerWord:
    text: str
    start: float
    end: float
    speaker: int
    language: str


@dataclass
class Utterance:
    speaker: int  # 0부터, 처음 말한 순서대로
    start: float
    end: float
    language: str  # "ko" / "en" / "mixed" / 그 외 언어
    text: str
    words: list[SpeakerWord] = field(default_factory=list)


# 말한 시간이 이보다 적은 화자는 잡음으로 보고 없앤다 (초, 전체 말 시간 대비 비율 중 큰 쪽).
# 강의에서 학생이 한 번 질문한 것(10초)은 남아야 하므로 아주 작게 잡는다. 같은 사람이 두 화자로
# 나뉜 경우는 화면의 '다른 화자와 합치기'로 사람이 고친다.
MINOR_SPEAKER_SECONDS = 3.0
MINOR_SPEAKER_SHARE = 0.002
# A A [B B] A A: 이만큼 짧게 끼어든 화자는 앞뒤 화자로 맞춘다
BLIP_MAX_WORDS = 3
BLIP_MAX_SECONDS = 1.5
# 문장이 안 끝났는데 이만큼 짧게 다른 화자가 붙으면 앞 화자 말이 이어진 것으로 본다 ("So it's" / "like")
CONTINUATION_MAX_WORDS = 2
CONTINUATION_MAX_SECONDS = 0.8


def assign_speakers(
    segments: list[Segment],
    activity: SpeakerActivity,
    num_speakers: Optional[int] = None,
) -> list[SpeakerWord]:
    """단어마다 그 시간 동안 확률이 가장 높은 화자를 붙인다.

    num_speakers 를 주면 말한 시간이 가장 긴 N명만 남기고, 나머지 화자의 단어는 남은 N명 중 하나로 보낸다.
    말한 시간이 아주 적은 화자는 오류로 보고 없앤 뒤 다시 붙인다.
    """
    allowed = np.arange(activity.probs.shape[1])
    if num_speakers:
        allowed = np.argsort(-activity.total_activity())[:num_speakers]

    words = sorted(
        ((w, seg.language) for seg in segments for w in (seg.words or [Word(seg.text, seg.start, seg.end)])),
        key=lambda x: x[0].start,
    )
    probs = [activity.mean_probs(w.start, w.end) for w, _ in words]

    def assign(allowed_idx: np.ndarray) -> list[SpeakerWord]:
        out = []
        for (w, lang), p_all in zip(words, probs):
            p = p_all[allowed_idx]
            spk = int(allowed_idx[int(np.argmax(p))]) if p.size and p.max() > 0.05 else -1
            out.append(SpeakerWord(w.text, w.start, w.end, spk, word_language(w.text, lang)))
        return out

    out = assign(allowed)
    minor = _minor_speakers(out)
    if minor and len(minor) < len(set(w.speaker for w in out if w.speaker != -1)):
        out = assign(np.array([a for a in allowed if int(a) not in minor]))

    _fill_unknown(out)
    _smooth(out)
    _attach_continuations(out)
    _renumber(out)
    return out


def _minor_speakers(words: list[SpeakerWord]) -> set[int]:
    talk: dict[int, float] = {}
    for w in words:
        if w.speaker != -1:
            talk[w.speaker] = talk.get(w.speaker, 0.0) + max(0.0, w.end - w.start)
    total = sum(talk.values())
    limit = max(MINOR_SPEAKER_SECONDS, MINOR_SPEAKER_SHARE * total)
    return {spk for spk, t in talk.items() if t < limit}


def _fill_unknown(words: list[SpeakerWord]) -> None:
    """화자 확률이 거의 없는 단어(-1)는 앞 단어 화자, 없으면 뒤 단어 화자를 따른다."""
    last = -1
    for w in words:
        if w.speaker == -1:
            w.speaker = last
        else:
            last = w.speaker
    nxt = -1
    for w in reversed(words):
        if w.speaker == -1:
            w.speaker = nxt if nxt != -1 else 0
        else:
            nxt = w.speaker


def _runs(words: list[SpeakerWord]) -> list[tuple[int, int]]:
    """같은 화자가 연달아 말한 단어 묶음 [(시작 인덱스, 끝 인덱스)]."""
    runs, i = [], 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1].speaker == words[i].speaker:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def _smooth(words: list[SpeakerWord]) -> None:
    """A A [B B] A A 처럼 짧게 튀는 화자는 앞뒤 화자로 맞춘다."""
    for i, j in _runs(words):
        if (
            0 < i and j < len(words) - 1
            and words[i - 1].speaker == words[j + 1].speaker
            and j - i + 1 <= BLIP_MAX_WORDS
            and words[j].end - words[i].start <= BLIP_MAX_SECONDS
        ):
            for k in range(i, j + 1):
                words[k].speaker = words[i - 1].speaker


def _attach_continuations(words: list[SpeakerWord]) -> None:
    """앞 화자의 문장이 안 끝났는데 아주 짧은 다른 화자 조각이 바로 붙으면 앞 화자 말로 본다."""
    for i, j in _runs(words):
        if i == 0:
            continue
        prev = words[i - 1]
        if (
            not prev.text.endswith(SENTENCE_END)
            and j - i + 1 <= CONTINUATION_MAX_WORDS
            and words[j].end - words[i].start <= CONTINUATION_MAX_SECONDS
            and words[i].start - prev.end < 0.5
        ):
            for k in range(i, j + 1):
                words[k].speaker = prev.speaker


def _renumber(words: list[SpeakerWord]) -> None:
    """화자 번호를 처음 등장한 순서대로 0, 1, 2… 로 바꾼다."""
    mapping: dict[int, int] = {}
    for w in words:
        if w.speaker not in mapping:
            mapping[w.speaker] = len(mapping)
        w.speaker = mapping[w.speaker]


def utterance_language(words: list[SpeakerWord]) -> str:
    langs = [w.language for w in words]
    top = max(set(langs), key=langs.count)
    ko, en = langs.count("ko"), langs.count("en")
    if ko and en and min(ko, en) / (ko + en) >= MIXED_MIN_SHARE:
        return "mixed"
    return top


def build_utterances(words: list[SpeakerWord]) -> list[Utterance]:
    """연속된 같은 화자 단어를 문단으로 묶는다."""
    utts: list[Utterance] = []
    cur: list[SpeakerWord] = []

    def flush():
        if not cur:
            return
        utts.append(Utterance(
            speaker=cur[0].speaker,
            start=cur[0].start,
            end=cur[-1].end,
            language=utterance_language(cur),
            text=" ".join(w.text for w in cur),
            words=list(cur),
        ))
        cur.clear()

    for w in words:
        if cur:
            prev = cur[-1]
            sentence_end = prev.text.endswith(SENTENCE_END)
            if (
                w.speaker != prev.speaker
                or (sentence_end and w.start - prev.end >= PARAGRAPH_PAUSE_SECONDS)
                or (sentence_end and prev.end - cur[0].start >= PARAGRAPH_MAX_SECONDS)
            ):
                flush()
        cur.append(w)
    flush()
    return utts
