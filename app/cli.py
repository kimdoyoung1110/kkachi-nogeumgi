"""개발용: 파일 하나를 전사해서 터미널에 출력한다.

    uv run python -m app.cli 파일.m4a [--lang auto|ko|en] [--hotwords "hash table,collision"] [--json out.json]
"""

import argparse
import json
import sys
import time
from dataclasses import asdict

from app import audio as audio_io
from app.stt import Transcriber


def fmt(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--lang", default="auto", choices=["auto", "ko", "en"])
    p.add_argument("--hotwords", default="", help="쉼표로 구분한 용어 목록")
    p.add_argument("--no-align", action="store_true")
    p.add_argument("--json", help="결과를 JSON으로 저장할 경로")
    args = p.parse_args()

    t0 = time.time()
    audio = audio_io.decode(args.file)
    dur = audio_io.duration(audio)
    t_decode = time.time() - t0

    tr = Transcriber()
    t0 = time.time()
    tr.asr, tr.aligner  # 모델 로딩 시간은 따로 잰다
    t_load = time.time() - t0

    stage_t = {}

    def progress(stage, done, total):
        stage_t.setdefault(stage, time.time())
        print(f"\r  {stage}: {done}/{total}", end="", file=sys.stderr, flush=True)

    t0 = time.time()
    segments = tr.transcribe(
        audio,
        language=None if args.lang == "auto" else args.lang,
        hotwords=[w.strip() for w in args.hotwords.split(",") if w.strip()] or None,
        progress=progress,
        align=not args.no_align,
    )
    t_total = time.time() - t0
    t_align = time.time() - stage_t["align"] if "align" in stage_t else 0.0
    print(file=sys.stderr)

    for seg in segments:
        print(f"[{fmt(seg.start)}] ({seg.language.upper()}) {seg.text}")

    print(
        f"\n오디오 {dur / 60:.1f}분 | 변환 {t_decode:.1f}s | 모델 로딩 {t_load:.1f}s | "
        f"전사 {t_total - t_align:.1f}s | 정렬 {t_align:.1f}s | 합계 {t_total:.1f}s "
        f"(실시간 대비 {dur / max(t_total, 1e-6):.0f}배)",
        file=sys.stderr,
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in segments], f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
