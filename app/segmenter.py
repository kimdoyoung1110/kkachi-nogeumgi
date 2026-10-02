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
