"""까치 영상분석기: 광고 영상을 컷으로 나누고 컷마다 화면·글자·내레이션을 모은다.

videos/<id>/video.mp4     받은 영상
videos/<id>/shots.json    {"id", "title", "source", "duration", "shots": [...], "speech": [...]}
videos/<id>/frames/       컷마다 시작·중간·끝 화면
videos/<id>/sheets/       컷 여러 개를 한 장에 모은 이미지 (Claude 에게 보낼 때 이미지 수를 줄인다)

전부 이 맥 안에서 돌아간다. 화면을 해석하는 일은 이 결과를 받은 Claude 가 한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import imageio_ffmpeg

from app import audio as audio_io
from app import config

log = logging.getLogger(__name__)

VIDEOS_DIR = config.DATA_DIR / "videos"
SCENE_THRESHOLD = 0.2      # 낮을수록 컷을 많이 찾는다 (광고는 컷이 빠르고 비슷한 화면이 이어져 낮게)
MIN_SHOT_SECONDS = 0.2     # 이보다 짧으면 번쩍임으로 보고 앞 컷에 붙인다
SHOTS_PER_SHEET = 6
THUMB = (360, 203)
WORKERS = 4
MAX_TEXT_LINES = 8

ProgressFn = Callable[[str], None]


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", *args],
                          capture_output=True, text=True, errors="replace")


def video_id(source: str) -> str:
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/)([\w-]{11})", source)
    return m.group(1) if m else hashlib.sha1(source.encode()).hexdigest()[:11]


def fetch(source: str, folder: Path) -> tuple[Path, str]:
    """주소면 내려받고, 내 맥의 파일이면 그대로 쓴다. (영상 경로, 제목)"""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "video.mp4"
    if not re.match(r"https?://", source):
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"영상 파일을 찾지 못했어요: {source}")
        return path, path.stem
    import yt_dlp

    options = {"format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b", "merge_output_format": "mp4",
               "outtmpl": str(folder / "video.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True,
               "noprogress": True, "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe()}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source, download=not target.exists())
    return target, info.get("title") or source


def detect_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """컷이 바뀌는 시각들 (초). 화면이 앞 장면과 얼마나 달라졌는지 ffmpeg 가 점수를 낸다."""
    out = _ffmpeg("-i", str(path), "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-").stderr
    return [float(t) for t in re.findall(r"pts_time:([\d.]+)", out)]


def make_shots(cuts: list[float], duration: float, min_seconds: float = MIN_SHOT_SECONDS) -> list[dict[str, float]]:
    edges = [0.0]
    for t in sorted(cuts):
        if t - edges[-1] >= min_seconds and duration - t >= min_seconds:
            edges.append(t)
    edges.append(duration)
    return [{"index": i + 1, "start": round(a, 3), "end": round(b, 3), "length": round(b - a, 3)}
            for i, (a, b) in enumerate(zip(edges, edges[1:]))]


def grab(path: Path, t: float, out: Path, width: int = 960) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    for back in (0.0, 0.2, 0.5, 1.0):   # 소리가 화면보다 길면 맨 끝에는 화면이 없어서 조금씩 앞으로 간다
        _ffmpeg("-y", "-ss", f"{max(0.0, t - back):.3f}", "-i", str(path), "-frames:v", "1",
                "-vf", f"scale={width}:-2", "-q:v", "3", str(out))
        if out.exists():
            return out
    raise ValueError(f"{t:.2f}초 화면을 꺼내지 못했어요.")


def read_text(image: Path) -> list[str]:
    """맥에 들어 있는 글자 인식(Vision)으로 화면 글자를 읽는다."""
    import objc
    import Vision
    from Foundation import NSURL

    with objc.autorelease_pool():   # 여러 스레드에서 돌리므로 스레드마다 정리한다
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["ko-KR", "en-US"])
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), None)
        ok, _ = handler.performRequests_error_([request], None)
        if not ok:
            return []
        return [str(r.topCandidates_(1)[0].string()) for r in (request.results() or []) if r.confidence() >= 0.4]


def speech_in(speech: list[dict[str, Any]], start: float, end: float) -> str:
    """그 컷과 시간이 겹치는 말. 단어 시각이 있으면 단어 단위로 고른다."""
    words = [w["text"] for u in speech for w in u["words"] if start <= (w["start"] + w["end"]) / 2 < end]
    return " ".join(words)


def make_sheets(folder: Path, shots: list[dict[str, Any]]) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    w, h = THUMB
    label = 110
    paths = []
    for page, first in enumerate(range(0, len(shots), SHOTS_PER_SHEET)):
        group = shots[first:first + SHOTS_PER_SHEET]
        sheet = Image.new("RGB", (label + w * 3 + 8, (h + 6) * len(group)), "#111111")
        draw = ImageDraw.Draw(sheet)
        for row, shot in enumerate(group):
            y = row * (h + 6)
            draw.text((8, y + 8), f"#{shot['index']}", fill="white", font=font)
            draw.text((8, y + 38), f"{shot['start']:.1f}s", fill="#bbbbbb", font=font)
            draw.text((8, y + 66), f"{shot['length']:.1f}s", fill="#888888", font=font)
            for col, name in enumerate(shot["frames"]):
                with Image.open(folder / name) as im:
                    sheet.paste(im.convert("RGB").resize(THUMB), (label + col * (w + 4), y))
        path = folder / "sheets" / f"sheet{page + 1:02d}.jpg"
        path.parent.mkdir(exist_ok=True)
        sheet.save(path, quality=62)
        paths.append(path)
    return paths


def _read_shot(path: Path, folder: Path, shot: dict[str, Any]) -> None:
    """컷 하나의 시작·중간·끝 화면을 꺼내고 글자를 읽는다."""
    a, b = shot["start"], shot["end"]
    margin = min(0.08, (b - a) / 4)
    shot["frames"], shot["text"] = [], []
    for tag, t in (("a", a + margin), ("b", (a + b) / 2), ("c", b - margin)):
        name = f"frames/shot{shot['index']:03d}_{tag}.jpg"
        grab(path, t, folder / name)
        shot["frames"].append(name)
        shot["text"] += [line for line in read_text(folder / name) if line not in shot["text"]]


def analyze(source: str, pipeline: Any = None, progress: Optional[ProgressFn] = None) -> dict[str, Any]:
    """영상 하나를 분석한다. 이미 분석한 영상이면 저장해 둔 결과를 돌려준다."""
    say = progress or (lambda _text: None)
    vid = video_id(source)
    folder = VIDEOS_DIR / vid
    meta_path = folder / "shots.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))

    say("영상 받는 중")
    path, title = fetch(source, folder)
    duration = audio_io.probe_duration(path)
    if not duration:
        raise ValueError("영상 길이를 읽지 못했어요. 영상 파일이 맞는지 확인해 주세요.")

    say("컷 나누는 중")
    shots = make_shots(detect_cuts(path), duration)

    say("컷별 화면과 글자 읽는 중")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(lambda shot: _read_shot(path, folder, shot), shots))

    speech: list[dict[str, Any]] = []
    if pipeline is not None:
        say("내레이션 받아쓰는 중")
        for u in pipeline.process(audio_io.decode(path)):
            speech.append({"start": u.start, "end": u.end, "text": u.text,
                           "words": [{"text": w.text, "start": w.start, "end": w.end} for w in u.words]})
    for shot in shots:
        shot["speech"] = speech_in(speech, shot["start"], shot["end"])

    make_sheets(folder, shots)
    meta = {"id": vid, "title": title, "source": source, "path": str(path), "duration": duration,
            "shots": shots, "speech": [{k: u[k] for k in ("start", "end", "text")} for u in speech]}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def load(vid: str) -> dict[str, Any]:
    path = VIDEOS_DIR / vid / "shots.json"
    if not path.exists():
        raise ValueError(f"분석한 영상 중에 {vid} 가 없어요. 먼저 analyze_video 를 불러 주세요.")
    return json.loads(path.read_text(encoding="utf-8"))


def sheets(vid: str) -> list[Path]:
    return sorted((VIDEOS_DIR / vid / "sheets").glob("sheet*.jpg"))


def frames_between(vid: str, start: float, end: float, count: int) -> list[tuple[float, Path]]:
    """구간을 고르게 나눠 화면을 꺼낸다. 움직임이나 전환을 촘촘히 볼 때 쓴다."""
    meta = load(vid)
    folder = VIDEOS_DIR / vid / "zoom"
    count = max(1, min(count, 12))
    times = [start + (end - start) * (i + 0.5) / count for i in range(count)]
    return [(t, grab(Path(meta["path"]), t, folder / f"t{t:08.3f}.jpg", width=720)) for t in times]


def shot_table(meta: dict[str, Any]) -> str:
    rows = [f"# {meta['title']}", f"영상 번호 {meta['id']} · 길이 {meta['duration']:.1f}초 · 컷 {len(meta['shots'])}개", "",
            "| 컷 | 시작 | 길이 | 화면 글자 | 내레이션 |", "|---|---|---|---|---|"]
    for s in meta["shots"]:
        lines = s["text"][:MAX_TEXT_LINES]   # 책 한 면처럼 글자가 가득한 화면은 앞부분만
        text = " / ".join(lines).replace("|", "\\|") + (" …" if len(s["text"]) > len(lines) else "")
        rows.append(f"| {s['index']} | {s['start']:.2f}s | {s['length']:.2f}s | {text} | {s['speech']} |")
    return "\n".join(rows)
