from PIL import Image, ImageSequence, ImageDraw
import numpy as np, math
from scipy import ndimage

def masks(f):
    a = np.array(f.convert("RGBA")).astype(int)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    op = al > 128
    body = op & (abs(r - g) < 14) & (abs(g - b) < 14) & (r >= 40) & (r <= 130)
    dark = op & (r < 45) & (g < 45) & (b < 45)
    orange = op & (r > 200) & (g > 80) & (g < 185) & (b < 110)
    return body, dark, orange, op

def blobs(mask, minpx, maxpx):
    lab, n = ndimage.label(mask)
    out = []
    for i in range(1, n + 1):
        ys, xs = np.nonzero(lab == i)
        if not (minpx <= len(xs) <= maxpx): continue
        h, w = np.ptp(ys) + 1, np.ptp(xs) + 1
        out.append(dict(x=xs.mean(), y=ys.mean(), n=len(xs), h=h, w=w, fill=len(xs) / (h * w)))
    return out

def anchor(f):
    body, dark, orange, op = masks(f)
    if body.sum() < 300: return None
    area = (body | dark | orange).sum()
    eyes = [b for b in blobs(dark, 120, 2500) if max(b["h"], b["w"]) / min(b["h"], b["w"]) < 1.7 and b["fill"] > 0.55]
    sil = op
    ys, xs = np.nonzero(sil)
    if eyes:
        if len(eyes) >= 2:
            eyes = sorted(eyes, key=lambda e: e["y"])[:2]
            ex, ey = np.mean([e["x"] for e in eyes]), np.mean([e["y"] for e in eyes])
            dx, dy = eyes[1]["x"] - eyes[0]["x"], eyes[1]["y"] - eyes[0]["y"]
            if dx < 0: dx, dy = -dx, -dy
            ang = math.atan2(dy, dx)            # 두 눈을 잇는 선의 기울기
            up = (math.sin(ang), -math.cos(ang))
            kind = "front"
        else:
            ex, ey = eyes[0]["x"], eyes[0]["y"]
            ors = blobs(orange, 60, 6000)
            near = [o for o in ors if math.hypot(o["x"] - ex, o["y"] - ey) < 150]
            if near:
                o = min(near, key=lambda o: math.hypot(o["x"] - ex, o["y"] - ey))
                vx, vy = ex - o["x"], ey - o["y"]         # 부리 → 눈 (머리 뒤쪽 방향)
                L = math.hypot(vx, vy) or 1
                vx, vy = vx / L, vy / L
                up = (vy, -vx) if vy * 1 <= vx * 0 or -vx < 0 else (-vy, vx)
                if up[1] > 0: up = (-up[0], -up[1])
                ex, ey = ex + vx * 18, ey + vy * 18      # 머리 중심은 눈보다 살짝 뒤
                kind = "side"
            else:
                up = (0.0, -1.0); kind = "eye-only"
        # 머리 중심에서 위쪽으로 걸어가며 몸통이 끝나는 지점 = 머리 꼭대기
        px, py = ex, ey
        for _ in range(400):
            nx, ny = px + up[0], py + up[1]
            if not (0 <= int(ny) < sil.shape[0] and 0 <= int(nx) < sil.shape[1]) or not sil[int(ny), int(nx)]:
                break
            px, py = nx, ny
        ang = math.degrees(math.atan2(up[0], -up[1]))
    else:
        # 눈이 안 보이면 몸통 맨 위
        top = ys.min()
        cols = xs[ys <= top + 6]
        px, py, ang, kind = cols.mean(), float(top), 0.0, "top"
    return dict(x=px, y=py, ang=max(-40, min(40, ang)), scale=math.sqrt(area), kind=kind)

if __name__ == "__main__" and False:
    frames = [f.convert("RGBA").copy() for f in ImageSequence.Iterator(Image.open("web/emoji/magpie.webp"))]
    res = [anchor(f) for f in frames]
    from collections import Counter
    print(Counter(r["kind"] for r in res if r))
    sheet = Image.new("RGBA", (12 * 160, 7 * 160), (255, 255, 255, 255))
    for i, f in enumerate(frames):
        t = f.copy(); d = ImageDraw.Draw(t); a = res[i]
        if a:
            s = a["scale"] * 0.22
            th = math.radians(a["ang"])
            # 모자 자리: 앵커에서 위로 사각형
            cx, cy = a["x"] + math.sin(th) * s * 0.3, a["y"] - math.cos(th) * s * 0.3
            d.ellipse([a["x"] - 8, a["y"] - 8, a["x"] + 8, a["y"] + 8], fill="red")
            d.line([a["x"], a["y"], a["x"] + math.sin(th) * s, a["y"] - math.cos(th) * s], fill="blue", width=6)
        t = t.resize((160, 160)); ImageDraw.Draw(t).text((4, 4), f"{i} {a['kind'] if a else ''}", fill="black")
        sheet.paste(t, ((i % 12) * 160, (i // 12) * 160), t)
    sheet.save("/private/tmp/claude-501/-Users-dozero-development-kkachi-nogeumgi/f6accd55-be30-4f1e-99c8-7e220354012b/scratchpad/anchors.png")
