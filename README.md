# 까치녹음기

맥(Apple Silicon)에서 **인터넷 없이** 녹음 → 전사 → 화자 구분까지 해주는 개인용 앱.
한국어 / 영어 / 한·영 혼합 강의를 지원한다.

## 구성
| 역할 | 사용 기술 |
|---|---|
| 전사 | Qwen3-ASR 1.7B (MLX, 8bit) via `mlx-audio` |
| 단어 타임스탬프 | Qwen3-ForcedAligner 0.6B (+ `soynlp` 한국어 토크나이저) |
| 화자 구분 | Nemotron 3 Diarization (MLX, 최대 8명) |
| 오디오 변환 | `imageio-ffmpeg` (Homebrew 불필요) |
| 서버 | FastAPI + uvicorn |
| 저장 | SQLite (`~/Library/Application Support/KkachiNogeumgi/`) |

## 개발 실행
```bash
uv sync
uv run uvicorn app.main:app --port 8765 --reload
```

## 폴더
```
app/       서버 코드
web/       화면 (HTML/JS)
scripts/   설치·실행·업데이트 스크립트
tests/     테스트 + 테스트용 음성(tests/audio)
docs/      계획·벤치마크 기록
```
