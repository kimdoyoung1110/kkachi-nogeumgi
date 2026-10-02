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
# 언어 감지 (한·영 혼합 모드): SpeechBrain ECAPA VoxLingua107 MLX 변환본. Apache-2.0, 약 80MB.
LANGID_MODEL = "beshkenadze/lang-id-voxlingua107-ecapa-mlx"
ASR_CHUNK_SECONDS = 30


def _ram_gb() -> float:
    try:
        import subprocess
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip()) / 2**30
    except Exception:
        return 16.0


RAM_GB = _ram_gb()
# 16GB 맥에서 8개씩 묶으면 최대 12GB 가까이 써서 맥 전체가 느려졌다 (66분 회의 실측) → 메모리에 맞춰 줄인다
ASR_BATCH_SIZE = int(os.environ.get("KKACHI_BATCH", 4 if RAM_GB <= 16.5 else 8))
# MLX 가 계산 후 들고 있는 임시 메모리 상한 (GB)
MLX_CACHE_LIMIT_GB = float(os.environ.get("KKACHI_MLX_CACHE_GB", 1.0 if RAM_GB <= 16.5 else 4.0))
SAMPLE_RATE = 16000


def ensure_dirs() -> None:
    for d in (DATA_DIR, RECORDINGS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
