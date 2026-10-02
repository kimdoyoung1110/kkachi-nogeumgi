"""구간 언어 감지: SpeechBrain ECAPA-TDNN (VoxLingua107, MLX 변환본).

Qwen3-ASR 자체 언어 감지는 30초 구간 첫머리 언어로 전체를 정해서, 영어 질문 뒤 한국어 답변을
영어로 '번역'해 버리는 문제가 있었다. 그래서 전사 전에 짧은 조각마다 언어를 따로 판정한다.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Optional

import numpy as np

from app import config

# 판정 대상 언어. 이 중에서만 고른다 (다른 언어로 튀는 것 방지).
ALLOWED = ("ko", "en")
# 1등 언어 확률(허용 언어끼리 재정규화)이 이보다 낮으면 '모름'으로 두고 앞뒤 조각을 따른다
MIN_CONFIDENCE = 0.7
# 이보다 짧은 조각은 판정하지 않는다. 웃음·맞장구·잡음 같은 짧은 소리를 영어로 잘못 보고
# 영어로 받아쓰게 하면 모델이 없는 영어 문장을 지어낸다 (실제 회의 녹음에서 23문단 발생)
MIN_SECONDS = 2.0
# 녹음의 이 비율 이상이 한 언어면 '주 언어'. 다른 언어는 아주 확실하고 충분히 길 때만 인정
DOMINANT_SHARE = 0.8
MINORITY_CONFIDENCE = 0.97
MINORITY_MIN_SECONDS = 3.0


class LanguageIdentifier:
    def __init__(self, repo: str = config.LANGID_MODEL):
        self.repo = repo
        self._model = None
        self._label_index: dict[str, int] = {}

    @property
    def model(self):
        if self._model is None:
            import mlx.core as mx
            from huggingface_hub import snapshot_download
            from mlx_audio.lid.models.ecapa_tdnn import Model, ModelConfig

            # mlx-audio 0.5.7 의 lid.load() 는 이 체크포인트 config 를 못 읽어서 직접 조립한다
            src = Path(snapshot_download(self.repo))
            cfg = json.loads((src / "config.json").read_text())
            names = {f.name for f in dataclasses.fields(ModelConfig)}
            model = Model(ModelConfig(**{k: v for k, v in cfg.items() if k in names}))
            weights = model.sanitize(mx.load(str(next(src.glob("*.safetensors")))))
            # 모델이 같은 블록을 두 이름으로 참조해서 strict 검사가 실패한다 (실제 누락 없음)
            model.load_weights(list(weights.items()), strict=False)
            model.eval()
            self._model = model
            # id2label 값은 "ko: Korean" 형식
            self._label_index = {
                v.split(":")[0].strip(): int(k) for k, v in cfg["id2label"].items()
            }
        return self._model

    def scores(self, audio: np.ndarray) -> dict[str, float]:
        """허용 언어끼리 재정규화한 확률."""
        import mlx.core as mx
        from mlx_audio.lid.models.ecapa_tdnn.mel import compute_mel_spectrogram

        model = self.model
        probs = np.array(mx.exp(model(compute_mel_spectrogram(mx.array(audio))))[0])
        raw = {lang: float(probs[self._label_index[lang]]) for lang in ALLOWED}
        total = sum(raw.values()) or 1.0
        return {k: v / total for k, v in raw.items()}

    def score_pieces(self, pieces: list[np.ndarray]) -> list[Optional[dict[str, float]]]:
        """조각마다 언어 확률. 너무 짧은 조각은 None."""
        sr = config.SAMPLE_RATE
        return [self.scores(a) if len(a) >= MIN_SECONDS * sr else None for a in pieces]


def decide(durations: list[float], scores: list[Optional[dict[str, float]]]) -> tuple[list[str], str]:
    """조각별 언어를 정한다. (조각별 언어, 녹음의 주 언어)

    1) 2초 이상이고 70% 이상 확실한 조각만 판정
    2) 녹음의 80% 이상이 한 언어면, 다른 언어는 97% 이상 확실하고 3초 이상일 때만 인정
    3) 판정 못 한 조각은 앞뒤 조각을 따른다
    """
    labels: list[Optional[str]] = []
    for s in scores:
        if not s:
            labels.append(None)
            continue
        lang = max(s, key=s.get)
        labels.append(lang if s[lang] >= MIN_CONFIDENCE else None)

    total: dict[str, float] = {}
    for d, lab in zip(durations, labels):
        if lab:
            total[lab] = total.get(lab, 0.0) + d
    dominant = max(total, key=total.get) if total else "ko"
    share = total.get(dominant, 0.0) / (sum(total.values()) or 1.0)

    if share >= DOMINANT_SHARE:
        for i, (d, lab) in enumerate(zip(durations, labels)):
            if lab and lab != dominant and not (scores[i][lab] >= MINORITY_CONFIDENCE and d >= MINORITY_MIN_SECONDS):
                labels[i] = dominant
    return fill_unknown(labels, default=dominant), dominant


def fill_unknown(labels: list[Optional[str]], default: str = "ko") -> list[str]:
    """판정 못 한 조각은 바로 앞 조각 언어를, 맨 앞이면 뒤 조각 언어를 따른다."""
    out: list[Optional[str]] = list(labels)
    last = None
    for i, lab in enumerate(out):
        if lab is None:
            out[i] = last
        else:
            last = lab
    nxt = None
    for i in range(len(out) - 1, -1, -1):
        if labels[i] is not None:
            nxt = labels[i]
        if out[i] is None:
            out[i] = nxt or default
    return out  # type: ignore[return-value]
