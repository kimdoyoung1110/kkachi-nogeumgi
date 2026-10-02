"""긴 오디오를 전사용 구간(최대 N초)으로 자른다. 자르는 위치는 가장 조용한 지점."""

from dataclasses import dataclass

import numpy as np

from app.config import SAMPLE_RATE

FRAME_SECONDS = 0.02
# 이 밑으로 조용한 구간은 전사하지 않는다 (무음에서 모델이 헛소리를 만드는 것 방지)
SILENCE_DBFS = -55.0


@dataclass(frozen=True)
class Span:
    start: int  # sample index
    end: int

    def seconds(self, sr: int = SAMPLE_RATE) -> tuple[float, float]:
        return self.start / sr, self.end / sr


def _frame_energy(audio: np.ndarray, frame: int) -> np.ndarray:
    n = len(audio) // frame
    if n == 0:
        return np.array([float(np.mean(audio**2))])
    frames = audio[: n * frame].reshape(n, frame)
    return np.mean(frames**2, axis=1)


def split(
    audio: np.ndarray,
    sr: int = SAMPLE_RATE,
    max_seconds: float = 30.0,
    search_seconds: float = 10.0,
) -> list[Span]:
    """max_seconds 를 넘지 않게 자르되, 끝에서 search_seconds 안의 가장 조용한 곳에서 자른다."""
    frame = int(FRAME_SECONDS * sr)
    energy = _frame_energy(audio, frame)
    max_frames = int(max_seconds / FRAME_SECONDS)
    search_frames = int(search_seconds / FRAME_SECONDS)
    total_frames = len(energy)

    spans: list[Span] = []
    start = 0
    while start < total_frames:
        end = start + max_frames
        if end >= total_frames:
            spans.append(Span(start * frame, len(audio)))
            break
        lo = max(start + 1, end - search_frames)
        cut = lo + int(np.argmin(energy[lo:end]))
        spans.append(Span(start * frame, cut * frame))
        start = cut
    return spans


def is_silent(chunk: np.ndarray) -> bool:
    if chunk.size == 0:
        return True
    rms = float(np.sqrt(np.mean(chunk**2)))
    return 20 * np.log10(rms + 1e-12) < SILENCE_DBFS


# ---- 화자 기준 구간 나누기 (혼합 모드) ----

# 같은 화자가 이보다 짧게 쉬면 한 턴으로 본다
TURN_MERGE_GAP = 0.5
# 이보다 짧은 화자 턴은 잡음으로 보고 앞 턴에 붙인다
TURN_MIN_SECONDS = 0.3
# 턴 사이 공백이 이보다 길면 공백을 별도 구간으로 떼어낸다 (대부분 무음이라 버려짐)
GAP_CARVE_SECONDS = 2.0


@dataclass(frozen=True)
class Turn:
    start: float  # 초
    end: float
    speaker: int


def turns_from_probs(probs: np.ndarray, frame_seconds: float, threshold: float = 0.5) -> list[Turn]:
    """프레임별 화자 확률 → 화자 턴 목록."""
    if probs.size == 0:
        return []
    active = probs.max(axis=1) > threshold
    spk = np.where(active, probs.argmax(axis=1), -1)

    runs: list[list] = []  # [start_frame, end_frame, speaker]
    i = 0
    while i < len(spk):
        j = i
        while j + 1 < len(spk) and spk[j + 1] == spk[i]:
            j += 1
        if spk[i] != -1:
            runs.append([i, j + 1, int(spk[i])])
        i = j + 1

    merge_gap = TURN_MERGE_GAP / frame_seconds
    min_len = TURN_MIN_SECONDS / frame_seconds
    merged: list[list] = []
    for r in runs:
        if merged and merged[-1][2] == r[2] and r[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = r[1]
        elif merged and r[1] - r[0] < min_len:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(r)
    return [Turn(s * frame_seconds, e * frame_seconds, k) for s, e, k in merged]


def split_by_turns(
    audio: np.ndarray,
    turns: list[Turn],
    sr: int = SAMPLE_RATE,
    max_seconds: float = 30.0,
    min_seconds: float = 10.0,
) -> list[Span]:
    """최대 max_seconds 구간으로 자르되, 자르는 위치는 화자가 바뀌는 지점을 우선한다.

    구간을 화자 턴 하나로 너무 짧게 자르면 앞뒤 맥락이 없어져 인식이 크게 나빠진다 (실험으로 확인).
    그래서 맥락은 30초까지 유지하고, 한 사람 말 중간이 아닌 화자 교체 지점에서 끊는다.
    턴 사이 긴 공백은 따로 떼어낸다 (무음이면 나중에 걸러진다).
    """
    if not turns:
        return split(audio, sr, max_seconds)
    total = len(audio) / sr

    # 화자가 바뀌는 지점 (공백 한가운데)
    changes = [
        (p.end + n.start) / 2 if n.start > p.end else n.start
        for p, n in zip(turns, turns[1:])
        if p.speaker != n.speaker and n.start - p.end < GAP_CARVE_SECONDS
    ]
    # 긴 공백 기준으로 큰 덩어리를 나눈다
    regions: list[tuple[float, float]] = []
    a = 0.0
    for p, n in zip(turns, turns[1:]):
        if n.start - p.end >= GAP_CARVE_SECONDS:
            regions += [(a, p.end + 0.2), (p.end + 0.2, n.start - 0.2)]
            a = n.start - 0.2
    regions.append((a, total))

    spans: list[Span] = []
    for ra, rb in regions:
        start = ra
        while rb - start > 0.1:
            end = start + max_seconds
            if end >= rb:
                cut = rb
            else:
                cands = [c for c in changes if start + min_seconds <= c <= end]
                if cands:
                    cut = cands[-1]
                else:
                    sub = split(audio[int(start * sr):int(rb * sr)], sr, max_seconds)
                    cut = start + sub[0].end / sr
            spans.append(Span(int(start * sr), int(cut * sr)))
            start = cut
    return spans


# ---- 언어 기준 구간 나누기 (혼합 모드) ----

LANG_PIECE_SECONDS = 8.0


@dataclass(frozen=True)
class Piece:
    """언어를 판정할 짧은 조각 (한 화자 턴 안)."""
    span: Span
    speaker: int


def pieces_from_turns(
    audio: np.ndarray, turns: list[Turn], sr: int = SAMPLE_RATE, max_seconds: float = LANG_PIECE_SECONDS
) -> list[Piece]:
    """화자 턴을 최대 max_seconds 조각으로 (조용한 곳에서) 자른다. 같은 사람이 중간에 언어를 바꾸는 경우를 잡기 위함."""
    pieces: list[Piece] = []
    for t in turns:
        a, b = int(t.start * sr), int(t.end * sr)
        if b - a <= 0:
            continue
        for sub in split(audio[a:b], sr, max_seconds, search_seconds=max_seconds / 2):
            pieces.append(Piece(Span(a + sub.start, a + sub.end), t.speaker))
    return pieces


def language_regions(
    pieces: list[Piece], labels: list[str], total_samples: int
) -> list[tuple[int, int, str]]:
    """언어가 같은 연속 조각을 묶어 [start, end) 샘플 구간으로 만든다. 구간들은 오디오 전체를 덮는다."""
    if not pieces:
        return []
    regions: list[list] = []
    for piece, lang in zip(pieces, labels):
        if regions and regions[-1][2] == lang:
            regions[-1][1] = piece.span.end
        else:
            if regions:
                # 언어가 바뀌는 곳: 앞 조각 끝과 이 조각 시작의 가운데
                mid = (regions[-1][1] + piece.span.start) // 2
                regions[-1][1] = mid
                regions.append([mid, piece.span.end, lang])
            else:
                regions.append([0, piece.span.end, lang])
    regions[-1][1] = total_samples
    return [(a, b, lang) for a, b, lang in regions if b > a]


def split_region_by_turns(
    audio: np.ndarray,
    turns: list[Turn],
    start: int,
    end: int,
    sr: int = SAMPLE_RATE,
    max_seconds: float = 30.0,
) -> list[Span]:
    """audio[start:end] 구간에 split_by_turns 를 적용하고 결과를 전체 기준 위치로 돌려준다."""
    off = start / sr
    local = [
        Turn(max(t.start, off) - off, min(t.end, end / sr) - off, t.speaker)
        for t in turns if t.end > off and t.start < end / sr
    ]
    return [Span(start + s.start, start + s.end)
            for s in split_by_turns(audio[start:end], local, sr, max_seconds)]
