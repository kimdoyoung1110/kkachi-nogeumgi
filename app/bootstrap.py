"""설치 도우미: 모델 내려받기와 시험 받아쓰기.

    .venv/bin/python -m app.bootstrap download   # 모델 4개 내려받기 (약 4GB)
    .venv/bin/python -m app.bootstrap selftest   # 시험 음성으로 끝까지 돌려보기
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from app import config

MODELS = [
    ("받아쓰기 (Qwen3-ASR 1.7B)", config.ASR_MODEL),
    ("단어 시간 맞추기 (Qwen3-ForcedAligner)", config.ALIGNER_MODEL),
    ("화자 구분 (Nemotron 3)", config.DIARIZATION_MODEL),
    ("언어 감지 (ECAPA)", config.LANGID_MODEL),
]
SAMPLE = Path(__file__).resolve().parent.parent / "tests" / "audio" / "mixed_ko_en.wav"


def download() -> int:
    from huggingface_hub import snapshot_download

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    for i, (label, repo) in enumerate(MODELS, 1):
        print(f"  [{i}/{len(MODELS)}] {label}", flush=True)
        for attempt in range(3):
            try:
                snapshot_download(repo)
                break
            except Exception as e:  # 네트워크가 잠깐 끊긴 경우 다시 시도
                if attempt == 2:
                    print(f"    ✗ 내려받지 못했어요: {e}", flush=True)
                    return 1
                print("    인터넷이 잠깐 끊겼어요. 다시 시도할게요…", flush=True)
                time.sleep(5)
    print("  ✓ 모델을 모두 받았어요.", flush=True)
    return 0


def selftest() -> int:
    os.environ["HF_HUB_OFFLINE"] = "1"  # 받아둔 모델만으로 동작하는지 확인
    from app import audio
    from app.pipeline import Pipeline

    t0 = time.time()
    pipe = Pipeline()
    pipe.warmup()
    t_load = time.time() - t0
    t0 = time.time()
    utts = pipe.process(audio.decode(SAMPLE))
    took = time.time() - t0
    for u in utts:
        print(f"    화자{u.speaker + 1} ({u.language.upper()}) {u.text[:60]}", flush=True)
    speakers = {u.speaker for u in utts}
    langs = {u.language for u in utts}
    ok = len(speakers) >= 2 and "en" in langs and "ko" in langs
    print(f"  모델 준비 {t_load:.0f}초 · 28초 음성 처리 {took:.1f}초", flush=True)
    print("  ✓ 시험 받아쓰기 성공" if ok else "  ✗ 시험 결과가 이상해요 (화자 2명, 한·영이 나와야 함)", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"download": download, "selftest": selftest}.get(cmd, lambda: (print(__doc__), 2)[1])())
