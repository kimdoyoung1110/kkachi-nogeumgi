"""앱 전역 설정. 녹음/DB 같은 사용자 데이터는 저장소 밖에 둔다."""

import os
from pathlib import Path

APP_NAME = "KkachiNogeumgi"

DATA_DIR = Path(
    os.environ.get(
        "KKACHI_DATA_DIR",
        Path.home() / "Library" / "Application Support" / APP_NAME,
    )
)
RECORDINGS_DIR = DATA_DIR / "recordings"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "kkachi.db"

HOST = "127.0.0.1"
PORT = int(os.environ.get("KKACHI_PORT", "8765"))

# 전사: Qwen3-ASR 1.7B (8bit, MLX). 30초 단위로 쪼개 8개씩 배치 처리.
ASR_MODEL = "mlx-community/Qwen3-ASR-1.7B-8bit"
ALIGNER_MODEL = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
# 화자 구분: NVIDIA Nemotron 3 Diarization (MLX). 토큰 불필요, 최대 8명.
DIARIZATION_MODEL = "mlx-community/Nemotron-3-Diarization"
ASR_CHUNK_SECONDS = 30
ASR_BATCH_SIZE = 8
SAMPLE_RATE = 16000


def ensure_dirs() -> None:
    for d in (DATA_DIR, RECORDINGS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
