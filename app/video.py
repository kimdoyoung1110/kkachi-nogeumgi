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
SHOTS_PER_SHEET = 6
THUMB = (360, 203)
WORKERS = 4
MAX_TEXT_LINES = 8

ProgressFn = Callable[[str], None]
STEPS = {"영상 받는 중": 0.05, "컷 나누는 중": 0.2, "컷별 화면과 글자 읽는 중": 0.3, "내레이션 받아쓰는 중": 0.65}
ID_RE = re.compile(r"^[\w-]{1,32}$")


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


def _root(root: Optional[Path]) -> Path:
    return root or VIDEOS_DIR


def set_status(folder: Path, state: str, step: str = "", error: Optional[str] = None, **extra: Any) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    old = read_json(folder / "status.json") or {}
    status = {**old, "state": state, "step": step, "progress": STEPS.get(step, 0.0), "error": error,
              "updated_at": time.time(), **extra}
    tmp = folder / "status.json.tmp"
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "status.json")


def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def analyze(source: str, pipeline: Any = None, progress: Optional[ProgressFn] = None, root: Optional[Path] = None,
            vid: Optional[str] = None, title: Optional[str] = None) -> dict[str, Any]:
    """영상 하나를 분석한다. 이미 분석한 영상이면 저장해 둔 결과를 돌려준다."""
    vid = vid or video_id(source)
    folder = _root(root) / vid
    meta_path = folder / "shots.json"
    done = read_json(meta_path)
    if done:
        return done

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
    except Exception as exc:
        set_status(folder, "failed", error=str(exc) if isinstance(exc, (ValueError, FileNotFoundError))
                   else "영상을 분석하지 못했어요. 다시 시도해 주세요.")
        raise
    meta = {"id": vid, "title": title or found_title, "source": source, "path": str(path), "duration": duration,
            "created_at": time.time(), "shots": shots,
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
    if status:
        return {"id": folder.name, "created_at": status.get("updated_at", 0), "thumb": None, "shots": 0, "duration": None,
                **status, "title": status.get("title") or status.get("source")}
    return None


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
    writer.writerow(["컷", "시작(초)", "끝(초)", "길이(초)", "화면 글자", "내레이션"])
    for s in meta["shots"]:
        writer.writerow([s["index"], s["start"], s["end"], s["length"], " / ".join(s["text"]), s["speech"]])
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
    thumb_w, thumb_h, gap = 366, 206, 8
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
        draw.text((margin, y), f"컷 {shot['index']}", fill="#1d1d1f", font=head_font)
        draw.text((margin + 110, y + 3), f"{shot['start']:.2f}초부터 · {shot['length']:.2f}초", fill="#5f5e5a", font=body_font)
        y += 38
        for col, name in enumerate(shot["frames"]):
            with Image.open(folder / name) as im:
                thumb = im.convert("RGB").resize((thumb_w, thumb_h))
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
        raise ValueError(f"없는 컷 번호예요: {missing}. 이 영상은 컷 1~{len(known)}까지 있어요.")
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
    return [(t, grab(Path(meta["path"]), t, folder / f"t{t:08.3f}.jpg", width=720)) for t in times]


def shot_table(meta: dict[str, Any]) -> str:
    rows = [f"# {meta['title']}", f"영상 번호 {meta['id']} · 길이 {meta['duration']:.1f}초 · 컷 {len(meta['shots'])}개", "",
            "| 컷 | 시작 | 길이 | 화면 글자 | 내레이션 |", "|---|---|---|---|---|"]
    for s in meta["shots"]:
        lines = s["text"][:MAX_TEXT_LINES]   # 책 한 면처럼 글자가 가득한 화면은 앞부분만
        text = " / ".join(lines).replace("|", "\\|") + (" …" if len(s["text"]) > len(lines) else "")
        rows.append(f"| {s['index']} | {s['start']:.2f}s | {s['length']:.2f}s | {text} | {s['speech']} |")
    return "\n".join(rows)
