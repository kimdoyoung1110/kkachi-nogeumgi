"""개발자용: 레벨별 까치 움직이는 그림을 만든다 (web/emoji/magpie-lv4~8.webp).

Noto 애니메이션 까치(magpie.webp)를 장면마다 분석해 머리 꼭대기를 찾고,
- 레벨에 맞게 깃털 색을 바꾸고 (남색 → 보랏빛 → 금빛 날개선 → 무지개)
- 학사모·왕관(이것도 움직이는 그림)을 머리에 씌운다.

    uv run --with pillow --with numpy --with scipy python scripts/make_level_art.py [--preview out.gif]
"""

from __future__ import annotations

import colorsys
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).parent))
from _level_anchor import anchor, blobs, masks  # noqa: E402

EMOJI = Path(__file__).resolve().parent.parent / "web" / "emoji"
PAD = 0.22          # 모자·여백을 위해 원본(512) 둘레에 붙이는 비율
OUT = 320           # 저장 크기

LEVELS = {
    4: dict(colors=None),
    5: dict(colors=((70, 92, 150), (32, 46, 92))),                  # 푸른빛 남색
    6: dict(colors=((112, 84, 196), (52, 38, 120)), hat="cap"),      # 보랏빛 남색 + 학사모
    7: dict(colors=((40, 52, 104), (16, 22, 58)), gold=True, hat="crown"),  # 짙은 남색 + 금빛 날개선 + 왕관
    8: dict(rainbow=True, gold=True, hat="crown"),                  # 무지개 + 금빛 + 왕관
}
HAT = {"cap": dict(width=0.62, sink=0.30), "crown": dict(width=0.56, sink=0.16)}


def load_frames(name):
    im = Image.open(EMOJI / name)
    frames, durs = [], []
    for f in ImageSequence.Iterator(im):
        frames.append(f.convert("RGBA").copy())
        durs.append(f.info.get("duration") or 30)
    return frames, durs


def smooth(anchors, win=2):
    """장면 사이 모자가 떨리지 않게 앞뒤 장면과 평균 (같은 종류의 장면끼리만)."""
    out = []
    for i, a in enumerate(anchors):
        near = [anchors[j] for j in range(max(0, i - win), min(len(anchors), i + win + 1))
                if anchors[j] and anchors[j]["kind"] == a["kind"]
                and math.hypot(anchors[j]["x"] - a["x"], anchors[j]["y"] - a["y"]) < 60]
        out.append(dict(a, x=np.mean([n["x"] for n in near]), y=np.mean([n["y"] for n in near]),
                        ang=np.mean([n["ang"] for n in near])))
    return out


def eye_mask(dark):
    m = np.zeros_like(dark)
    lab, n = ndimage.label(dark)
    for i in range(1, n + 1):
        comp = lab == i
        ys, xs = np.nonzero(comp)
        if not (60 <= len(xs) <= 2500):
            continue
        h, w = np.ptp(ys) + 1, np.ptp(xs) + 1
        if max(h, w) / min(h, w) < 1.7 and len(xs) / (h * w) > 0.5:
            m |= comp
    return m


def recolor(f, lv, frame_no):
    cfg = LEVELS[lv]
    if not (cfg.get("colors") or cfg.get("rainbow") or cfg.get("gold")):
        return f
    a = np.array(f).astype(float)
    body, dark, orange, op = masks(f)
    H, W = body.shape
    yy, xx = np.mgrid[0:H, 0:W]
    t = yy / H
    if cfg.get("rainbow"):
        hue = ((xx / W) * 0.55 + (yy / H) * 0.25 + frame_no * 0.012) % 1.0
        rgb = np.stack(np.vectorize(lambda h: colorsys.hsv_to_rgb(h, 0.62, 0.62), otypes=[float, float, float])(hue), -1) * 255
    else:
        top, bot = np.array(cfg["colors"], dtype=float)
        rgb = top[None, None, :] * (1 - t[..., None]) + bot[None, None, :] * t[..., None]
    a[..., :3][body] = rgb[body]
    if cfg.get("gold"):
        strokes = dark & ~eye_mask(dark)
        gold = np.array([236, 184, 52], float) * (1 - t[..., None]) + np.array([196, 132, 24], float) * t[..., None]
        a[..., :3][strokes] = gold[strokes]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8), "RGBA")


def hat_frames(name):
    frames, _ = load_frames(f"{name}.webp")
    # 모든 장면을 합친 테두리로 잘라야 장면마다 모자가 흔들리지 않는다
    box = None
    for f in frames:
        b = f.getbbox()
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    return [f.crop(box) for f in frames]


def put_hat(canvas, hat, a, off, cfg):
    s = a["scale"]
    width = s * cfg["width"]
    hat = hat.resize((max(1, int(width)), max(1, int(width * hat.height / hat.width))), Image.LANCZOS)
    # 모자 아래 가운데를 회전 중심으로: 큰 정사각형 가운데에 그 점이 오게 붙이고 돌린다
    side = int(max(hat.size) * 2.2)
    sq = Image.new("RGBA", (side, side))
    sq.paste(hat, (side // 2 - hat.width // 2, side // 2 - hat.height), hat)
    sq = sq.rotate(-a["ang"], resample=Image.BICUBIC)
    th = math.radians(a["ang"])
    up = (math.sin(th), -math.cos(th))
    sink = hat.height * cfg["sink"]
    px, py = a["x"] + off - up[0] * sink, a["y"] + off - up[1] * sink
    canvas.alpha_composite(sq, (int(px - side / 2), int(py - side / 2)))


def build(lv, base, durs, anchors, hats):
    cfg = LEVELS[lv]
    off = int(512 * PAD)
    size = 512 + off * 2
    out = []
    for i, f in enumerate(base):
        c = Image.new("RGBA", (size, size))
        c.alpha_composite(recolor(f, lv, i), (off, off))
        if cfg.get("hat") and anchors[i]:
            hf = hats[cfg["hat"]]
            put_hat(c, hf[i % len(hf)], anchors[i], off, HAT[cfg["hat"]])
        out.append(c.resize((OUT, OUT), Image.LANCZOS))
    return out


def main():
    base, durs = load_frames("magpie.webp")
    anchors = smooth([anchor(f) for f in base])
    hats = {"cap": hat_frames("cap"), "crown": hat_frames("crown")}
    results = {}
    for lv in (4, 5, 6, 7, 8):
        frames = build(lv, base, durs, anchors, hats)
        path = EMOJI / f"magpie-lv{lv}.webp"
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=durs, loop=0,
                       quality=82, method=6, lossless=False)
        results[lv] = frames
        print(f"{path.name}: {path.stat().st_size // 1024}KB")

    if "--preview" in sys.argv:
        dest = sys.argv[sys.argv.index("--preview") + 1]
        cell, lab_h = 220, 0
        sheet_frames = []
        for i in range(len(base)):
            sheet = Image.new("RGBA", (cell * 5, cell), (246, 245, 242, 255))
            for k, lv in enumerate((4, 5, 6, 7, 8)):
                fr = results[lv][i].resize((cell, cell), Image.LANCZOS)
                sheet.alpha_composite(fr, (k * cell, 0))
            sheet_frames.append(sheet.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=200))
        sheet_frames[0].save(dest, save_all=True, append_images=sheet_frames[1:], duration=durs, loop=0, optimize=True)
        print("preview:", dest, Path(dest).stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
