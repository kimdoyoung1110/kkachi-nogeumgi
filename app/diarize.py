"""화자 구분: NVIDIA Nemotron 3 Diarization (MLX). 최대 8명, 10ms 단위 화자별 확률을 준다."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app import config


@dataclass
class SpeakerActivity:
    probs: np.ndarray  # (frames, max_speakers) 프레임마다 화자별 말하고 있을 확률
    frame_seconds: float

    def mean_probs(self, start: float, end: float) -> np.ndarray:
        """[start, end) 동안의 화자별 평균 확률."""
        a = int(start / self.frame_seconds)
        b = max(a + 1, int(np.ceil(end / self.frame_seconds)))
        window = self.probs[a:b]
        if window.size == 0:
            return np.zeros(self.probs.shape[1], dtype=np.float32)
        return window.mean(axis=0)

    def total_activity(self) -> np.ndarray:
        """화자별로 말한 총 시간(초)."""
        return (self.probs > 0.5).sum(axis=0) * self.frame_seconds


class Diarizer:
    def __init__(self, model: str = config.DIARIZATION_MODEL):
        self.model_name = model
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from mlx_audio.vad import load

            self._model = load(self.model_name, strict=True)
        return self._model

    def run(self, audio: np.ndarray) -> SpeakerActivity:
        m = self.model
        out = m.generate(audio, sample_rate=config.SAMPLE_RATE)
        frame_seconds = (
            m._processor_config.hop_length * m.config.output_subsampling_factor / m.sample_rate
        )
        return SpeakerActivity(np.array(out.speaker_probs, dtype=np.float32), frame_seconds)
