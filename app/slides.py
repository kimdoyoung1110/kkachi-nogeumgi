"""녹음에 붙여 둔 강의자료(PDF): 쪽별 글자를 꺼내고, 받아쓴 문단마다 '지금 설명 중인 쪽'을 찾는다.

recordings/<id>/slides.pdf   원본 PDF
recordings/<id>/slides.json  {"name", "pages": [쪽별 글자], "map": {문단 id: 쪽}, "key": 맞춘 전사문 표시}

쪽 맞추기:
1. 쪽마다 낱말(영어 단어, 한글은 두 글자씩 조각)을 뽑고, 모든 쪽에 나오는 말(과목명·꼬리말)은 가볍게 친다 (TF-IDF).
2. 문단(+앞뒤 문단)의 낱말과 각 쪽의 낱말이 얼마나 겹치는지 점수를 낸다.
3. 강의는 대개 앞으로 넘어가므로, '같은 쪽에 머물기 > 다음 쪽 > 멀리 뛰기' 순으로 쉽게 해서
   전체 흐름이 가장 그럴듯한 쪽 순서를 고른다 (비터비). 한두 문장이 엉뚱한 쪽과 겹쳐도 흔들리지 않는다.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from app.terms import EN_STOP

PDF_NAME = "slides.pdf"
META_NAME = "slides.json"
MAX_BYTES = 150 * 1024 * 1024
MAX_PAGES = 600

STAY, NEXT, JUMP = 0.0, 0.35, 1.2   # 쪽을 옮기는 '비용'. 점수(겹침)가 이보다 커야 옮긴다
EMIT = 4.0                          # 겹침 점수(0~1)를 비용과 견줄 수 있게 키우는 배율


def page_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = reader.pages[:MAX_PAGES]
        return [(p.extract_text() or "").strip() for p in pages]
    except Exception as e:
        raise ValueError("PDF 를 열 수 없어요. 암호가 걸려 있거나 손상된 파일일 수 있어요.") from e


def tokens(text: str) -> list[str]:
    """비교용 낱말: 영어 단어(흔한 말 제외)와 한글 두 글자 조각 ('부조화' → 부조, 조화).
    조각으로 쪼개면 조사가 붙어도('부조화는') 같은 말로 잡힌다."""
    text = text.lower()
    out = [w for w in re.findall(r"[a-z][a-z0-9]{2,}", text) if w not in EN_STOP]
    for word in re.findall(r"[가-힣]{2,}", text):
        out += [word[i:i + 2] for i in range(len(word) - 1)]
    return out


def transcript_key(utterances: list[dict[str, Any]]) -> str:
    h = hashlib.sha1()
    for u in utterances:
        h.update(f"{u['id']}:{u['text']}\n".encode())
    return h.hexdigest()


def align(pages: list[str], utterances: list[dict[str, Any]]) -> dict[int, int]:
    """문단 id → 쪽 번호(1부터). 글자가 있는 쪽이 없거나 문단이 없으면 빈 dict."""
    if not utterances or not any(p.strip() for p in pages):
        return {}
    n = len(pages)
    page_tf = [Counter(tokens(p)) for p in pages]
    df: Counter[str] = Counter()
    for tf in page_tf:
        df.update(tf.keys())
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
    page_vec = []
    for tf in page_tf:
        vec = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        page_vec.append({t: v / norm for t, v in vec.items()})
    index: dict[str, list[tuple[int, float]]] = {}
    for p, vec in enumerate(page_vec):
        for t, v in vec.items():
            index.setdefault(t, []).append((p, v))

    utt_tokens = [tokens(u["text"]) for u in utterances]
    sims: list[list[float]] = []
    for i in range(len(utterances)):
        # 한 문단은 짧아서 앞뒤 문단을 조금 섞는다
        tf: Counter[str] = Counter(utt_tokens[i])
        for j in (i - 1, i + 1):
            if 0 <= j < len(utterances):
                for t in utt_tokens[j]:
                    tf[t] += 0.5
        q = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items() if t in idf and c >= 1}
        if c_norm := math.sqrt(sum(v * v for v in q.values())):
            q = {t: v / c_norm for t, v in q.items()}
        row = [0.0] * n
        for t, qv in q.items():
            for p, pv in index[t]:
                row[p] += qv * pv
        sims.append(row)

    # 비터비: score[p] = 지금까지 p 쪽에 있을 때 가장 좋은 점수
    score = [EMIT * s - (JUMP if p else 0.0) for p, s in enumerate(sims[0])]
    back: list[list[int]] = []
    for row in sims[1:]:
        best_prev = max(range(n), key=score.__getitem__)
        new, ptr = [0.0] * n, [0] * n
        for p in range(n):
            cands = [(score[p] - STAY, p), (score[best_prev] - JUMP, best_prev)]
            if p > 0:
                cands.append((score[p - 1] - NEXT, p - 1))
            val, src = max(cands)
            new[p], ptr[p] = val + EMIT * row[p], src
        score = new
        back.append(ptr)
    p = max(range(n), key=score.__getitem__)
    path = [p]
    for ptr in reversed(back):
        p = ptr[p]
        path.append(p)
    path.reverse()
    return {u["id"]: path[i] + 1 for i, u in enumerate(utterances)}


# ---- 저장 ----

def load_meta(rec_dir: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads((rec_dir / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(rec_dir: Path, name: str, data: bytes) -> dict[str, Any]:
    pages = page_texts(data)
    if not pages:
        raise ValueError("쪽이 없는 PDF 예요.")
    rec_dir.mkdir(parents=True, exist_ok=True)
    tmp = rec_dir / (PDF_NAME + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(rec_dir / PDF_NAME)
    meta = {"name": name, "pages": pages, "map": {}, "key": None}
    _write_meta(rec_dir, meta)
    return meta


def remove(rec_dir: Path) -> None:
    for name in (PDF_NAME, META_NAME):
        (rec_dir / name).unlink(missing_ok=True)


def info(rec_dir: Path, utterances: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """화면에 줄 정보. 전사문이 바뀌었으면 쪽 맞추기를 다시 해서 저장해 둔다."""
    meta = load_meta(rec_dir)
    if meta is None or not (rec_dir / PDF_NAME).exists():
        return None
    key = transcript_key(utterances)
    if meta.get("key") != key:
        meta["map"] = {str(k): v for k, v in align(meta["pages"], utterances).items()}
        meta["key"] = key
        _write_meta(rec_dir, meta)
    return {
        "name": meta["name"],
        "pages": len(meta["pages"]),
        "has_text": any(p.strip() for p in meta["pages"]),
        "map": meta["map"],
    }


def search(rec_dir: Path, query: str) -> list[dict[str, Any]]:
    meta = load_meta(rec_dir)
    if not meta:
        return []
    q = query.lower()
    hits = []
    for i, text in enumerate(meta["pages"]):
        flat = re.sub(r"\s+", " ", text)
        j = flat.lower().find(q)
        if j >= 0:
            hits.append({"page": i + 1, "text": flat[max(0, j - 40): j + len(q) + 60]})
    return hits


def _write_meta(rec_dir: Path, meta: dict[str, Any]) -> None:
    tmp = rec_dir / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(rec_dir / META_NAME)
