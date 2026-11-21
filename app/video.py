"""까치 영상분석기: 광고 영상을 컷으로 나누고 컷마다 화면·글자·내레이션을 모은다.

videos/<id>/video.mp4     받은 영상
videos/<id>/shots.json    {"id", "title", "source", "duration", "shots": [...], "speech": [...]} 끝나면 생긴다
videos/<id>/status.json   {"state": queued/running/failed, "step", "progress", "error"} 처리 중에만 있다
videos/<id>/frames/       컷마다 시작·중간·끝 화면
videos/<id>/sheets/       컷 여러 개를 한 장에 모은 이미지 (Claude 에게 보낼 때 이미지 수를 줄인다)

전부 이 맥 안에서 돌아간다. 화면을 해석하는 일은 이 결과를 받은 Claude 가 한다.
앱의 영상 탭과 Claude 앱(video_mcp)이 같은 폴더를 쓰므로 어느 쪽에서 분석해도 양쪽에서 보인다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
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
FRAME_SIZE = 960           # 꺼내는 화면의 긴 쪽 길이 (가로·세로 영상 모두 비율 그대로)
STALE_SECONDS = 2 * 3600   # 어느 프로그램이 분석하는지 모르는 옛 기록은 이만큼 소식이 없으면 멈춘 걸로 본다
WORKERS = 4
MAX_TEXT_LINES = 8

ProgressFn = Callable[[str], None]
STEPS = {"영상 받는 중": 0.05, "컷 나누는 중": 0.2, "컷별 화면과 글자 읽는 중": 0.3, "내레이션 받아쓰는 중": 0.65}
ID_RE = re.compile(r"^[\w-]{1,32}$")


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", *args],
                          capture_output=True, text=True, errors="replace")


def video_id(source: str) -> str:
    """같은 영상이면 주소 뒤에 붙은 공유 표시(?si=, ?igsh= 등)가 달라도 같은 번호."""
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/)([\w-]{11})", source)
    if m:
        return m.group(1)
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", source)
    if m:
        return f"ig_{m.group(1)}"[:32]
    return hashlib.sha1(source.encode()).hexdigest()[:11]


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

    # 화질은 짧은 쪽 기준 1080 까지 (세로 릴스 720×1280 도 고화질로). 소리가 없는 영상도 받는다
    options = {"format": "bv*+ba/bv*/b", "format_sort": ["res:1080", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"], "merge_output_format": "mp4",
               "outtmpl": str(folder / "video.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True,
               "noprogress": True, "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe()}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source, download=not target.exists())
    except yt_dlp.utils.DownloadError as exc:
        log.warning("영상 받기 실패: %s", exc)
        raise ValueError("영상을 받지 못했어요. 주소가 맞는지, 누구나 볼 수 있는 영상인지 확인해 주세요. "
                         "계속 안 되면 영상 파일로 올려 주세요.") from exc
    return target, info.get("title") or source


def detect_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """컷이 바뀌는 시각들 (초). 화면이 앞 장면과 얼마나 달라졌는지 ffmpeg 가 점수를 낸다."""
    out = _ffmpeg("-i", str(path), "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-").stderr
    return [float(t) for t in re.findall(r"pts_time:([\d.]+)", out)]


SPLITS = {"auto": "자동 (컷 + 3초 넘는 장면은 1초씩)", "cut": "컷마다", "0.5": "0.5초마다", "1": "1초마다", "2": "2초마다"}
LONG_SHOT = 3.0    # 자동: 이보다 긴 장면(카메라가 끊김 없이 움직이는 릴스 등)은
AUTO_STEP = 1.0    #       이 간격으로 더 쪼갠다
MAX_PIECES = 400   # 긴 영상을 잘게 자를 때 구간이 너무 많아지지 않게


def split_shots(shots: list[dict[str, Any]], split: str = "auto") -> list[dict[str, Any]]:
    """컷을 시간 간격으로 더 나눈다. 구간마다 원래 컷 번호(cut)와 그 안의 순서(part/parts)를 남긴다."""
    if split not in SPLITS:
        raise ValueError(f"나누는 방식은 {', '.join(SPLITS)} 중 하나예요.")

    def pieces(step: Optional[float]) -> list[dict[str, Any]]:
        out = []
        for cut, s in enumerate(shots, 1):
            length = s["end"] - s["start"]
            use = step if split != "auto" or length > LONG_SHOT else None
            n = max(1, round(length / use)) if use else 1
            for k in range(n):
                a, b = s["start"] + length * k / n, s["start"] + length * (k + 1) / n
                out.append({"start": round(a, 3), "end": round(b, 3), "length": round(b - a, 3),
                            "cut": cut, "part": k + 1, "parts": n})
        return out

    step = None if split == "cut" else AUTO_STEP if split == "auto" else float(split)
    out = pieces(step)
    if step and len(out) > MAX_PIECES:   # 너무 많으면 간격을 넓힌다
        out = pieces(step * len(out) / MAX_PIECES)
    for i, piece in enumerate(out, 1):
        piece["index"] = i
    return out


def label(shot: dict[str, Any]) -> str:
    """원래 컷 번호. 쪼갠 구간이면 '3-2' (3번 컷의 둘째 구간)."""
    cut = shot.get("cut", shot["index"])
    return f"{cut}-{shot['part']}" if shot.get("parts", 1) > 1 else str(cut)


def make_shots(cuts: list[float], duration: float, min_seconds: float = MIN_SHOT_SECONDS) -> list[dict[str, float]]:
    edges = [0.0]
    for t in sorted(cuts):
        if t - edges[-1] >= min_seconds and duration - t >= min_seconds:
            edges.append(t)
    edges.append(duration)
    return [{"index": i + 1, "start": round(a, 3), "end": round(b, 3), "length": round(b - a, 3)}
            for i, (a, b) in enumerate(zip(edges, edges[1:]))]


def grab(path: Path, t: float, out: Path, size: int = FRAME_SIZE) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    for back in (0.0, 0.2, 0.5, 1.0):   # 소리가 화면보다 길면 맨 끝에는 화면이 없어서 조금씩 앞으로 간다
        _ffmpeg("-y", "-ss", f"{max(0.0, t - back):.3f}", "-i", str(path), "-frames:v", "1",
                "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-q:v", "3", str(out))
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


def fit(image: Any, box: tuple[int, int]) -> Any:
    """비율을 지켜 box 안에 넣고 남는 곳은 검게 (세로 영상이 찌그러지거나 잘리지 않게)."""
    from PIL import Image

    scale = min(box[0] / image.width, box[1] / image.height)
    image = image.convert("RGB").resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    canvas = Image.new("RGB", box, "black")
    canvas.paste(image, ((box[0] - image.width) // 2, (box[1] - image.height) // 2))
    return canvas


def frame_aspect(folder: Path, shots: list[dict[str, Any]]) -> float:
    """가로 ÷ 세로. 세로 영상(릴스·쇼츠)이면 1보다 작다."""
    from PIL import Image

    with Image.open(folder / shots[0]["frames"][0]) as im:
        return im.width / im.height


def make_sheets(folder: Path, shots: list[dict[str, Any]]) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    tall = frame_aspect(folder, shots) < 1
    w, h = (203, 360) if tall else (360, 203)
    per_sheet = 4 if tall else 6   # 한 장이 너무 길어지지 않게
    label_w = 110
    paths = []
    for page, first in enumerate(range(0, len(shots), per_sheet)):
        group = shots[first:first + per_sheet]
        sheet = Image.new("RGB", (label_w + w * 3 + 8, (h + 6) * len(group)), "#111111")
        draw = ImageDraw.Draw(sheet)
        for row, shot in enumerate(group):
            y = row * (h + 6)
            draw.text((8, y + 8), f"#{shot['index']}", fill="white", font=font)
            if shot.get("parts", 1) > 1:
                draw.text((8, y + 94), f"cut {label(shot)}", fill="#888888", font=font)
            draw.text((8, y + 38), f"{shot['start']:.1f}s", fill="#bbbbbb", font=font)
            draw.text((8, y + 66), f"{shot['length']:.1f}s", fill="#888888", font=font)
            for col, name in enumerate(shot["frames"]):
                with Image.open(folder / name) as im:
                    sheet.paste(fit(im, (w, h)), (label_w + col * (w + 4), y))
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


def _root(root: Optional[Path]) -> Path:
    return root or VIDEOS_DIR


def set_status(folder: Path, state: str, step: str = "", error: Optional[str] = None, **extra: Any) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    old = read_json(folder / "status.json") or {}
    status = {**old, "state": state, "step": step, "progress": STEPS.get(step, 0.0), "error": error,
              "updated_at": time.time(), "pid": os.getpid(), **extra}
    tmp = folder / "status.json.tmp"
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "status.json")


def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def analyze(source: str, pipeline: Any = None, progress: Optional[ProgressFn] = None, root: Optional[Path] = None,
            vid: Optional[str] = None, title: Optional[str] = None, split: str = "auto") -> dict[str, Any]:
    """영상 하나를 분석한다. 같은 방식으로 이미 분석한 영상이면 저장해 둔 결과를 돌려준다.
    split: 'auto'(컷 + 긴 장면은 1초씩) / 'cut'(컷마다) / '0.5'·'1'·'2'(그 초마다)."""
    if split not in SPLITS:
        raise ValueError(f"나누는 방식은 {', '.join(SPLITS)} 중 하나예요.")
    vid = vid or video_id(source)
    folder = _root(root) / vid
    meta_path = folder / "shots.json"
    done = read_json(meta_path)
    if done and done.get("split", "cut") == split:   # 예전 결과는 컷마다 나눈 것
        return done
    if done:   # 다른 방식으로 다시 나눈다. 받아 둔 영상은 그대로 쓴다
        title = title or done["title"]
        meta_path.unlink()
        for name in ("frames", "sheets", "zoom"):
            shutil.rmtree(folder / name, ignore_errors=True)

    def say(step: str) -> None:
        set_status(folder, "running", step, source=source)
        if progress:
            progress(step)

    try:
        say("영상 받는 중")
        path, found_title = fetch(source, folder)
        duration = audio_io.probe_duration(path)
        if not duration:
            raise ValueError("영상 길이를 읽지 못했어요. 영상 파일이 맞는지 확인해 주세요.")

        say("컷 나누는 중")
        shots = split_shots(make_shots(detect_cuts(path), duration), split)

        say("컷별 화면과 글자 읽는 중")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda shot: _read_shot(path, folder, shot), shots))

        speech: list[dict[str, Any]] = []
        if pipeline is not None:
            say("내레이션 받아쓰는 중")
            try:
                sound = audio_io.decode(path)
            except audio_io.AudioDecodeError:
                sound = None   # 소리가 없는 영상 (음악·말 없는 릴스 등): 화면만 분석한다
            for u in pipeline.process(sound) if sound is not None and len(sound) else []:
                speech.append({"start": u.start, "end": u.end, "text": u.text,
                               "words": [{"text": w.text, "start": w.start, "end": w.end} for w in u.words]})
        for shot in shots:
            shot["speech"] = speech_in(speech, shot["start"], shot["end"])

        make_sheets(folder, shots)
    except Exception as exc:
        set_status(folder, "failed", error=str(exc) if isinstance(exc, (ValueError, FileNotFoundError))
                   else "영상을 분석하지 못했어요. 다시 시도해 주세요.")
        raise
    meta = {"id": vid, "title": title or found_title, "source": source, "path": str(path), "duration": duration,
            "aspect": round(frame_aspect(folder, shots), 4), "split": split, "created_at": time.time(), "shots": shots,
            "speech": [{k: u[k] for k in ("start", "end", "text")} for u in speech]}
    tmp = folder / "shots.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(meta_path)
    (folder / "status.json").unlink(missing_ok=True)
    return meta


def valid_id(vid: str) -> bool:
    return bool(ID_RE.match(vid))


def summary(folder: Path) -> Optional[dict[str, Any]]:
    """목록에 보일 내용. 끝난 영상이든 처리 중인 영상이든 같은 모양으로."""
    meta = read_json(folder / "shots.json")
    if meta:
        shots = meta["shots"]
        return {"id": meta["id"], "state": "done", "title": meta["title"], "source": meta["source"],
                "duration": meta["duration"], "shots": len(shots), "created_at": meta.get("created_at", 0),
                "thumb": shots[0]["frames"][1] if shots else None}
    status = read_json(folder / "status.json")
    if status and status.get("state") in ("queued", "running") and _stopped(status):
        status = {**status, "state": "failed", "error": "분석하던 프로그램이 꺼져서 멈췄어요. 지우고 다시 넣어 주세요."}
    if status:
        return {"id": folder.name, "created_at": status.get("updated_at", 0), "thumb": None, "shots": 0, "duration": None,
                **status, "title": status.get("title") or status.get("source")}
    return None


def _stopped(status: dict[str, Any]) -> bool:
    """분석하던 프로그램(앱 또는 Claude 앱의 도구)이 꺼졌는지."""
    pid = status.get("pid")
    if not pid:
        return time.time() - status.get("updated_at", 0) > STALE_SECONDS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def list_videos(root: Optional[Path] = None) -> list[dict[str, Any]]:
    base = _root(root)
    if not base.exists():
        return []
    found = [s for s in (summary(d) for d in base.iterdir() if d.is_dir()) if s]
    return sorted(found, key=lambda s: s.get("created_at") or 0, reverse=True)


def recover(root: Optional[Path] = None) -> None:
    """앱이 꺼지면서 멈춘 분석은 실패로 표시한다."""
    for item in list_videos(root):
        if item["state"] in ("queued", "running"):
            set_status(_root(root) / item["id"], "failed", error="앱이 꺼져서 멈췄어요. 지우고 다시 넣어 주세요.")


def delete(vid: str, root: Optional[Path] = None) -> None:
    shutil.rmtree(_root(root) / vid, ignore_errors=True)


def to_csv(meta: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["번호", "컷", "시작(초)", "끝(초)", "길이(초)", "화면 글자", "내레이션"])
    for s in meta["shots"]:
        writer.writerow([s["index"], label(s), s["start"], s["end"], s["length"], " / ".join(s["text"]), s["speech"]])
    return "\ufeff" + out.getvalue()   # 엑셀에서 한글이 안 깨지게


def _wrap(draw: Any, text: str, font: Any, width: int) -> list[str]:
    """한글은 띄어쓰기가 없어도 글자 단위로 줄을 나눈다."""
    lines, line = [], ""
    for ch in text:
        if draw.textlength(line + ch, font=font) > width:
            lines.append(line)
            line = ch.lstrip()
        else:
            line += ch
    return lines + [line] if line else lines


def make_pdf(meta: dict[str, Any], folder: Path, out: Path) -> Path:
    """샷 리스트 PDF: 컷마다 시작·중간·끝 스냅샷과 화면 글자, 내레이션. A4 세로."""
    from PIL import Image, ImageDraw, ImageFont

    page_w, page_h, margin = 1240, 1754, 60     # A4, 150dpi
    fonts = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
    title_font, head_font, body_font = (ImageFont.truetype(fonts, size) for size in (34, 24, 21))
    tall = meta.get("aspect", 16 / 9) < 1
    thumb_w, thumb_h, gap = (206, 366, 8) if tall else (366, 206, 8)
    pages: list[Any] = []

    def new_page() -> Any:
        pages.append(Image.new("RGB", (page_w, page_h), "white"))
        return ImageDraw.Draw(pages[-1])

    draw = new_page()
    y = margin
    for line in _wrap(draw, meta["title"], title_font, page_w - margin * 2):
        draw.text((margin, y), line, fill="#1d1d1f", font=title_font)
        y += 46
    draw.text((margin, y + 2), f"{meta['duration']:.1f}초 · 컷 {len(meta['shots'])}개", fill="#5f5e5a", font=body_font)
    y += 54

    for shot in meta["shots"]:
        notes = []
        if shot["text"]:
            kept = shot["text"][:MAX_TEXT_LINES]
            notes += _wrap(draw, "화면 글자: " + " / ".join(kept) + (" …" if len(shot["text"]) > len(kept) else ""),
                           body_font, page_w - margin * 2)
        if shot["speech"]:
            notes += _wrap(draw, "내레이션: " + shot["speech"], body_font, page_w - margin * 2)
        height = 38 + thumb_h + 8 + len(notes) * 30 + 22
        if y + height > page_h - margin:
            draw = new_page()
            y = margin
        draw.text((margin, y), f"{shot['index']} · 컷 {label(shot)}" if shot.get("parts", 1) > 1 else f"컷 {shot['index']}",
                  fill="#1d1d1f", font=head_font)
        draw.text((margin + 110, y + 3), f"{shot['start']:.2f}초부터 · {shot['length']:.2f}초", fill="#5f5e5a", font=body_font)
        y += 38
        for col, name in enumerate(shot["frames"]):
            with Image.open(folder / name) as im:
                thumb = fit(im, (thumb_w, thumb_h))
            pages[-1].paste(thumb, (margin + col * (thumb_w + gap), y))
        y += thumb_h + 8
        for line in notes:
            draw.text((margin, y), line, fill="#333333", font=body_font)
            y += 30
        y += 22
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150, quality=80)
    return out


def pick_shots(meta: dict[str, Any], numbers: Optional[list[int]]) -> dict[str, Any]:
    """고른 컷만 남긴 사본. numbers 가 없으면 전부."""
    if not numbers:
        return meta
    known = {s["index"] for s in meta["shots"]}
    missing = sorted(set(numbers) - known)
    if missing:
        raise ValueError(f"없는 번호예요: {missing}. 이 영상은 1~{len(known)}번까지 있어요.")
    return {**meta, "shots": [s for s in meta["shots"] if s["index"] in set(numbers)]}


def find_pdf(name: str, folders: list[Path]) -> Path:
    """파일 경로 또는 이름 일부로 PDF 를 찾는다. 여러 개면 고르도록 알려 준다."""
    direct = Path(name).expanduser()
    if direct.is_absolute():
        if direct.suffix.lower() == ".pdf" and direct.is_file():
            return direct
        raise FileNotFoundError(f"PDF 파일을 찾지 못했어요: {name}")
    word = name.removesuffix(".pdf").lower()
    found = [p for folder in folders if folder.is_dir() for p in folder.glob("*.pdf") if word in p.stem.lower()]
    if not found:
        raise FileNotFoundError(f"'{name}' 이름이 들어간 PDF 를 찾지 못했어요. 파일 전체 경로를 알려 주세요.")
    if len(found) > 1:
        raise ValueError("이름이 맞는 PDF 가 여러 개예요. 전체 경로로 골라 주세요:\n" + "\n".join(str(p) for p in found))
    return found[0]


def join_pdfs(original: Path, added: Path, out: Path) -> Path:
    """original 뒤에 added 를 붙여 out 에 새로 만든다. 원본 파일은 건드리지 않는다."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.append(str(original))
    writer.append(str(added))
    writer.write(str(out))
    writer.close()
    return out


def free_path(path: Path) -> Path:
    """같은 이름의 파일이 있으면 덮어쓰지 않게 번호를 붙인다."""
    n, found = 2, path
    while found.exists():
        found = path.with_name(f"{path.stem} {n}{path.suffix}")
        n += 1
    return found


def pdf_name(meta: dict[str, Any]) -> str:
    return re.sub(r'[/\\:*?"<>|]', " ", meta["title"]).strip()[:80] + " 샷 리스트.pdf"


def load(vid: str, root: Optional[Path] = None) -> dict[str, Any]:
    meta = read_json(_root(root) / vid / "shots.json") if valid_id(vid) else None
    if meta is None:
        raise ValueError(f"분석한 영상 중에 {vid} 가 없어요. 먼저 analyze_video 를 불러 주세요.")
    return meta


def sheets(vid: str) -> list[Path]:
    return sorted((VIDEOS_DIR / vid / "sheets").glob("sheet*.jpg"))


def frames_between(vid: str, start: float, end: float, count: int) -> list[tuple[float, Path]]:
    """구간을 고르게 나눠 화면을 꺼낸다. 움직임이나 전환을 촘촘히 볼 때 쓴다."""
    meta = load(vid)
    folder = VIDEOS_DIR / vid / "zoom"
    count = max(1, min(count, 12))
    times = [start + (end - start) * (i + 0.5) / count for i in range(count)]
    return [(t, grab(Path(meta["path"]), t, folder / f"t{t:08.3f}.jpg", size=720)) for t in times]


def shot_table(meta: dict[str, Any]) -> str:
    cuts = len({s.get("cut", s["index"]) for s in meta["shots"]})
    how = SPLITS.get(meta.get("split", "cut"), "")
    rows = [f"# {meta['title']}", f"영상 번호 {meta['id']} · 길이 {meta['duration']:.1f}초 · 컷 {cuts}개 · 구간 {len(meta['shots'])}개 ({how})",
            "", "| 번호 | 컷 | 시작 | 길이 | 화면 글자 | 내레이션 |", "|---|---|---|---|---|---|"]
    for s in meta["shots"]:
        lines = s["text"][:MAX_TEXT_LINES]   # 책 한 면처럼 글자가 가득한 화면은 앞부분만
        text = " / ".join(lines).replace("|", "\\|") + (" …" if len(s["text"]) > len(lines) else "")
        rows.append(f"| {s['index']} | {label(s)} | {s['start']:.2f}s | {s['length']:.2f}s | {text} | {s['speech']} |")
    return "\n".join(rows)
