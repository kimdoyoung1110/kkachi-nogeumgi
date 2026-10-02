"""전사 결과(단어 + 시간)와 화자 구분 결과를 합쳐 '누가 무슨 말을 했나' 문단을 만든다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.diarize import SpeakerActivity
from app.stt import Segment, Word

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
    language: str
    text: str
    words: list[SpeakerWord] = field(default_factory=list)


def assign_speakers(
    segments: list[Segment],
    activity: SpeakerActivity,
    num_speakers: Optional[int] = None,
) -> list[SpeakerWord]:
    """단어마다 그 시간 동안 확률이 가장 높은 화자를 붙인다.

    num_speakers 를 주면 말한 시간이 가장 긴 N명만 남기고, 나머지 화자의 단어는 남은 N명 중 하나로 보낸다.
    """
    allowed = np.arange(activity.probs.shape[1])
    if num_speakers:
        allowed = np.argsort(-activity.total_activity())[:num_speakers]

    out: list[SpeakerWord] = []
    for seg in segments:
        words = seg.words or [Word(seg.text, seg.start, seg.end)]
        for w in words:
            p = activity.mean_probs(w.start, w.end)[allowed]
            spk = int(allowed[int(np.argmax(p))]) if p.max() > 0.05 else -1
            out.append(SpeakerWord(w.text, w.start, w.end, spk, seg.language))

    _fill_unknown(out)
    _smooth(out)
    _renumber(out)
    return out


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


def _smooth(words: list[SpeakerWord], max_words: int = 1, max_seconds: float = 0.6) -> None:
    """A A [B] A A 처럼 짧게 튀는 화자는 앞뒤 화자로 맞춘다."""
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1].speaker == words[i].speaker:
            j += 1
        run_len = j - i + 1
        run_dur = words[j].end - words[i].start
        if (
            0 < i and j < len(words) - 1
            and words[i - 1].speaker == words[j + 1].speaker
            and run_len <= max_words and run_dur <= max_seconds
        ):
            for k in range(i, j + 1):
                words[k].speaker = words[i - 1].speaker
        i = j + 1


def _renumber(words: list[SpeakerWord]) -> None:
    """화자 번호를 처음 등장한 순서대로 0, 1, 2… 로 바꾼다."""
    mapping: dict[int, int] = {}
    for w in words:
        if w.speaker not in mapping:
            mapping[w.speaker] = len(mapping)
        w.speaker = mapping[w.speaker]


def build_utterances(words: list[SpeakerWord]) -> list[Utterance]:
    """연속된 같은 화자 단어를 문단으로 묶는다."""
    utts: list[Utterance] = []
    cur: list[SpeakerWord] = []

    def flush():
        if not cur:
            return
        langs = [w.language for w in cur]
        lang = max(set(langs), key=langs.count)
        utts.append(Utterance(
            speaker=cur[0].speaker,
            start=cur[0].start,
            end=cur[-1].end,
            language=lang,
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
