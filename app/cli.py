"""개발용: 파일 하나를 처리해서 화자별로 터미널에 출력한다.

    uv run python -m app.cli 파일.m4a [--lang auto|ko|en] [--speakers N]
                                       [--hotwords "hash table,collision"] [--json out.json]
"""

import argparse
import json
import sys
import time
from dataclasses import asdict

from app import audio as audio_io
from app.pipeline import Pipeline


def fmt(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--lang", default="auto", choices=["auto", "ko", "en"])
    p.add_argument("--speakers", type=int, default=None, help="화자 수 (모르면 생략)")
    p.add_argument("--hotwords", default="", help="쉼표로 구분한 용어 목록")
    p.add_argument("--json", help="결과를 JSON으로 저장할 경로")
    args = p.parse_args()

    t0 = time.time()
    audio = audio_io.decode(args.file)
    dur = audio_io.duration(audio)
    t_decode = time.time() - t0

    pipe = Pipeline()
    t0 = time.time()
    pipe.warmup()
    t_load = time.time() - t0

    marks: dict[str, float] = {}

    def progress(stage, done, total):
        marks.setdefault(stage, time.time())
        if done == total:
            marks[stage + "_end"] = time.time()
        print(f"\r  {stage}: {done}/{total}   ", end="", file=sys.stderr, flush=True)

    t0 = time.time()
    utterances = pipe.process(
        audio,
        language=None if args.lang == "auto" else args.lang,
        hotwords=[w.strip() for w in args.hotwords.split(",") if w.strip()] or None,
        num_speakers=args.speakers,
        progress=progress,
    )
    t_total = time.time() - t0
    print(file=sys.stderr)

    def took(stage):
        return marks.get(stage + "_end", 0) - marks.get(stage, 0)

    for u in utterances:
        print(f"[{fmt(u.start)}] 화자{u.speaker + 1} ({u.language.upper()}) {u.text}")

    n_spk = len({u.speaker for u in utterances})
    print(
        f"\n오디오 {dur / 60:.1f}분 | 화자 {n_spk}명 | 변환 {t_decode:.1f}s | 모델 로딩 {t_load:.1f}s | "
        f"화자구분 {took('diarize'):.1f}s | 전사 {took('transcribe'):.1f}s | 정렬 {marks.get('align_end', 0) - marks.get('transcribe_end', 0):.1f}s | "
        f"합계 {t_total:.1f}s (실시간 대비 {dur / max(t_total, 1e-6):.0f}배)",
        file=sys.stderr,
        flush=True,
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(u) for u in utterances], f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
