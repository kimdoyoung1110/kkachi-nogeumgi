"""받아쓴 결과를 파일로 내보낸다: 텍스트(.txt) / 마크다운(.md) / 자막(.srt)."""

from __future__ import annotations

import time
from typing import Any

# 자막 한 줄(큐) 기준: 이보다 길면 단어 시간으로 나눈다
SRT_MAX_SECONDS = 7.0
SRT_MAX_CHARS = 42

FORMATS = {
    "txt": ("text/plain; charset=utf-8", ".txt"),
    "md": ("text/markdown; charset=utf-8", ".md"),
    "srt": ("application/x-subrip; charset=utf-8", ".srt"),
}


def clock(sec: float, millis: bool = False) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if millis:
        return f"{h:02d}:{m:02d}:{s:02d},{int(round((sec - int(sec)) * 1000)) % 1000:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _date(ts: float) -> str:
    t = time.localtime(ts)
    days = "월화수목금토일"
    return f"{t.tm_year}년 {t.tm_mon}월 {t.tm_mday}일 ({days[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}"


def _duration(sec: float | None) -> str:
    if not sec:
        return ""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분 {s}초" if m else f"{s}초"


def _names(transcript: dict[str, Any]) -> dict[int, str]:
    return {s["idx"]: s["name"] for s in transcript["speakers"]}


def _notes_by_utt(transcript: dict[str, Any]) -> dict[int, list[str]]:
    """녹음 중 메모 → 그 시간에 말하던 문단 번호(순서)별 메모 글"""
    utts = transcript["utterances"]
    out: dict[int, list[str]] = {}
    for n in transcript.get("notes", []):
        i = 0
        for j, u in enumerate(utts):
            if u["start"] <= n["t"] + 0.3:
                i = j
            else:
                break
        out.setdefault(i, []).append(n["text"])
    return out


def to_txt(rec: dict[str, Any], transcript: dict[str, Any]) -> str:
    names = _names(transcript)
    notes = _notes_by_utt(transcript)
    lines = [rec["title"], " · ".join(x for x in (_date(rec["created_at"]), _duration(rec["duration"])) if x), ""]
    for i, u in enumerate(transcript["utterances"]):
        lines.append(f"[{clock(u['start'])}] {names.get(u['speaker'], f'화자 {u['speaker'] + 1}')}")
        lines.append(u["text"])
        lines += [f"📝 메모: {t}" for t in notes.get(i, [])]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_md(rec: dict[str, Any], transcript: dict[str, Any]) -> str:
    names = _names(transcript)
    meta = [_date(rec["created_at"]), _duration(rec["duration"])]
    if rec.get("subject"):
        meta.append(f"과목: {rec['subject']}")
    notes = _notes_by_utt(transcript)
    out = [f"# {rec['title']}", "", " · ".join(x for x in meta if x), ""]
    for i, u in enumerate(transcript["utterances"]):
        name = names.get(u["speaker"], f"화자 {u['speaker'] + 1}")
        out += [f"**{name}** `{clock(u['start'])}`", "", u["text"], ""]
        out += [f"> 📝 {t}\n" for t in notes.get(i, [])]
    return "\n".join(out).rstrip() + "\n"


def _cues(u: dict[str, Any]) -> list[tuple[float, float, str]]:
    """문단 하나를 자막 큐 여러 개로. 단어 시간이 있으면 길이·글자 수 기준으로 나눈다."""
    words = u["words"]
    if not words:
        return [(u["start"], u["end"], u["text"])]
    cues: list[tuple[float, float, str]] = []
    cur: list[list] = []
    for w in words:
        if cur:
            text = " ".join(x[0] for x in cur + [w])
            too_long = w[2] - cur[0][1] > SRT_MAX_SECONDS or len(text) > SRT_MAX_CHARS
            if too_long:
                cues.append((cur[0][1], cur[-1][2], " ".join(x[0] for x in cur)))
                cur = []
        cur.append(w)
    if cur:
        cues.append((cur[0][1], cur[-1][2], " ".join(x[0] for x in cur)))
    return cues


def to_srt(rec: dict[str, Any], transcript: dict[str, Any]) -> str:
    names = _names(transcript)
    blocks = []
    n = 0
    last_speaker = None
    for u in transcript["utterances"]:
        for i, (s, e, text) in enumerate(_cues(u)):
            n += 1
            # 화자가 바뀌는 첫 줄에만 이름을 붙인다
            if i == 0 and u["speaker"] != last_speaker:
                text = f"{names.get(u['speaker'], f'화자 {u['speaker'] + 1}')}: {text}"
            blocks.append(f"{n}\n{clock(s, True)} --> {clock(max(e, s + 0.5), True)}\n{text}\n")
        last_speaker = u["speaker"]
    return "\n".join(blocks)


RENDERERS = {"txt": to_txt, "md": to_md, "srt": to_srt}


def render(fmt: str, rec: dict[str, Any], transcript: dict[str, Any]) -> str:
    return RENDERERS[fmt](rec, transcript)
