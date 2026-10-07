"""강의자료(PDF·PPT·워드·텍스트)에서 받아쓰기 힌트로 쓸 전공 용어를 뽑는다.

받아쓰기 모델은 한국어 일상어는 잘 알아듣지만, 강의에 나오는 영어 전공 용어나
고유명사는 자주 틀린다. 그래서 강의자료에 자주 나오는 영어 용어·약어와,
'인지 부조화(cognitive dissonance)'처럼 괄호로 짝지어 적힌 말을 우선으로 고른다.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

MAX_BYTES = 80 * 1024 * 1024
MAX_PDF_PAGES = 400
SUFFIXES = (".pdf", ".pptx", ".docx", ".txt", ".md")

# 영어 흔한 말 + 강의자료에 늘 나오지만 용어는 아닌 말
EN_STOP = set("""
a about above after again against all also although always am among an and another any are around as at
be became because become been before being below between both but by can cannot could did do does doing done
down during each either else enough etc even ever every few first for from further get gets getting given go
goes going good got great had has have having he her here hers herself him himself his how however i if in
into is it its itself just last least less let like likely made make makes making many may me might more
most much must my myself near need new next no nor not now of off often on once one only or other others
our ours out over own part per perhaps put rather really same see seen several shall she should show shown
since so some such than that the their theirs them themselves then there therefore these they thing things
this those though through thus to too toward towards two under until up upon us use used uses using very via
was way ways we well were what when where whether which while who whom whose why will with within without
would yes yet you your yours
example examples figure figures fig chapter chapters page pages slide slides table tables section sections
note notes introduction summary review question questions answer answers lecture lectures class week weeks
today objective objectives learning learn key topic topics overview outline agenda contents source sources
reference references ref refs case cases study studies exercise exercises discussion homework assignment
quiz exam midterm final professor student students university college course department chapter
copyright rights reserved inc ltd co www http https com org edu pdf ppt
""".split())

KO_STOP = set("""
그리고 그러나 그래서 하지만 때문 경우 다음 이것 그것 저것 여기 거기 우리 사람 정도 이번 지난 오늘 내용 부분
예시 예제 문제 정답 설명 정리 요약 목차 학습 목표 강의 수업 교수 학생 과제 시험 중간 기말 참고 자료 그림 표
""".split())

EN_WORD = r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*"


# ---- 글자 꺼내기 ----

def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix == ".pptx":
        return _ooxml_text(data, r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$")
    if suffix == ".docx":
        return _ooxml_text(data, r"word/document\.xml$")
    if suffix in (".txt", ".md"):
        for enc in ("utf-8", "cp949"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="ignore")
    raise ValueError("PDF, 파워포인트(pptx), 워드(docx), 텍스트 파일만 읽을 수 있어요.")


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join((p.extract_text() or "") for p in pages)
    except Exception as e:  # 깨진 PDF·암호 걸린 PDF
        raise ValueError("PDF 를 열 수 없어요. 암호가 걸려 있거나 손상된 파일일 수 있어요.") from e


def _ooxml_text(data: bytes, pattern: str) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("파일을 열 수 없어요. 손상된 파일일 수 있어요.") from e
    names = sorted((n for n in z.namelist() if re.search(pattern, n)),
                   key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    out = []
    for n in names:
        root = ElementTree.fromstring(z.read(n))
        # 문단(a:p, w:p)마다 한 줄. 글자 조각(a:t, w:t)은 이어 붙인다
        for p in root.iter():
            if p.tag.endswith("}p"):
                line = "".join(t.text or "" for t in p.iter() if t.tag.endswith("}t"))
                if line.strip():
                    out.append(line)
    return "\n".join(out)


# ---- 용어 고르기 ----

def extract_terms(text: str, limit: int = 40) -> list[str]:
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # 줄 끝에서 잘린 영어 단어 잇기
    text = text.replace(" ", " ")
    scores: Counter[str] = Counter()
    shown: dict[str, str] = {}                            # 소문자 → 자료에 적힌 모양

    def add(term: str, score: float) -> None:
        term = re.sub(r"\s+", " ", term).strip(" -'·,.:;")
        if len(term) < 2 or len(term) > 50:
            return
        key = term.lower()
        scores[key] += score
        # 대문자 약어·고유명사 모양을 살린다 (같은 말이 여러 모양이면 대문자가 많은 쪽)
        if key not in shown or sum(c.isupper() for c in term) > sum(c.isupper() for c in shown[key]):
            shown[key] = term

    # 1) '한글(English)' / 'English(한글)' 짝: 자료가 직접 알려준 용어라 가장 확실하다
    for ko, en in re.findall(r"([가-힣][가-힣 ]{0,14}[가-힣])\s*\(\s*([A-Za-z][A-Za-z0-9 \-']{1,40}?)\s*\)", text):
        if _good_en_phrase(en):
            add(en, 6)
            add(ko, 3)
    for en, ko in re.findall(r"([A-Za-z][A-Za-z0-9 \-']{1,40}?)\s*\(\s*([가-힣][가-힣 ]{0,14}[가-힣])\s*\)", text):
        en = " ".join(en.split()[-4:])
        if _good_en_phrase(en):
            add(en, 6)
            add(ko, 3)

    words = re.findall(EN_WORD, text)
    counts = Counter(w.lower() for w in words)

    # 2) 약어 (CRM, B2B, ROI, LLM ...)
    for w in words:
        if re.fullmatch(r"[A-Z][A-Z0-9]{1,6}s?", w) and w.lower() not in EN_STOP and not w.isdigit():
            add(w, 2)

    # 3) 여러 단어 용어: 대문자로 시작하는 말 묶음 + 두 번 넘게 나온 두 단어 묶음
    for line in text.splitlines():
        for m in re.finditer(rf"(?:{EN_WORD})(?:[ ](?:{EN_WORD})){{1,3}}", line):
            phrase = m.group(0)
            ws = phrase.split()
            if all(w[0].isupper() for w in ws) and _good_en_phrase(phrase):
                add(phrase, 2.5)
        tokens = re.findall(EN_WORD, line)
        for a, b in zip(tokens, tokens[1:]):
            if _good_en_phrase(f"{a} {b}") and len(a) > 2 and len(b) > 2:
                scores_key = f"{a} {b}".lower()
                if _phrase_count(text, scores_key) >= 2:
                    add(f"{a} {b}", 1)

    # 4) 사람 이름이 붙은 이론 (Maslow's hierarchy), 하이픈으로 이은 말 (word-of-mouth)
    for name, rest in re.findall(rf"\b([A-Z][a-z]+)'s((?: (?:{EN_WORD})){{0,2}})", text):
        add(name, 1.5)
        rest_words = [w for w in rest.split() if w.lower() not in EN_STOP]
        if rest_words:
            add(f"{name}'s {rest_words[0]}" if rest.split()[0] == rest_words[0] else name, 1.5)
    for w in words:
        if "-" in w and len(w) > 5 and not w.lower().startswith(("e-mail",)):
            add(w, 1.5)

    # 5) 한 단어: 두 번 넘게 나온 흔하지 않은 영어 단어
    for w in words:
        lw = w.lower()
        if lw in EN_STOP or len(lw) < 4 or lw.isdigit() or re.fullmatch(r"[A-Z][A-Z0-9]{1,6}s?", w):
            continue
        if counts[lw] >= 2:
            add(w, 0.6)

    english = _ranked(scores, shown, lambda k: re.search(r"[a-z]", k) is not None)
    # 여러 단어 용어 안에 이미 들어 있는 한 단어는 빼서 자리를 아낀다
    phrases = [t.lower() for t in english if " " in t]
    english = [t for t in english if " " in t or not any(re.search(rf"\b{re.escape(t.lower())}\b", p) for p in phrases)]

    korean = _korean_terms(text)
    paired_ko = [shown[k] for k, v in scores.most_common() if re.fullmatch(r"[가-힣 ]+", k) and v >= 3]

    out: list[str] = []
    for t in paired_ko[:10] + english[:30] + korean:
        if t.lower() not in {o.lower() for o in out}:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _good_en_phrase(phrase: str) -> bool:
    ws = phrase.split()
    if not ws or len(ws) > 4:
        return False
    if ws[0].lower() in EN_STOP or ws[-1].lower() in EN_STOP:
        return False
    return any(len(w) > 2 for w in ws) and not all(w.isdigit() for w in ws)


def _phrase_count(text: str, phrase_lower: str) -> int:
    return len(re.findall(rf"\b{re.escape(phrase_lower)}\b", text.lower()))


def _ranked(scores: Counter, shown: dict[str, str], keep) -> list[str]:
    return [shown[k] for k, v in scores.most_common() if v > 0 and keep(k) and v >= 1.2]


JOSA = sorted("""은 는 이 가 을 를 의 에 에서 에게 으로 로 와 과 도 만 까지 부터 보다 처럼 이란 란 이다 입니다 이며 이고 일수록 할수록""".split(),
              key=len, reverse=True)


def _strip_josa(tok: str) -> str:
    for j in JOSA:
        if tok.endswith(j) and len(tok) - len(j) >= 2:
            return tok[: -len(j)]
    return tok


def _korean_terms(text: str, limit: int = 15) -> list[str]:
    """한국어 전공 용어: 자료에 두 번 넘게 나온 3글자 이상 낱말과 두 낱말 묶음 ('습관적 구매').
    흔한 2글자 낱말은 받아쓰기도 잘 하니 굳이 넣지 않는다."""
    uni: Counter[str] = Counter()
    bi: Counter[str] = Counter()
    for line in text.splitlines():
        for part in re.split(r"[^가-힣 ]", line):     # 영어·숫자·문장부호에서 끊는다
            raw = part.split()
            toks = [_strip_josa(t) for t in raw]
            for t in toks:
                uni[t] += 1
            for i in range(len(toks) - 1):
                a, b = toks[i], toks[i + 1]
                # 앞 낱말에 조사가 붙어 있으면 ('탐색이 많다') 한 용어가 아니다
                if len(a) >= 2 and len(b) >= 2 and raw[i] == a:
                    bi[f"{a} {b}"] += 1

    def ok(term: str) -> bool:
        ws = term.split()
        return not any(w in KO_STOP for w in ws) and not re.search(r"(다|며|고|면|서|는데|하는|되는|한|된|할|될)$", ws[-1])

    scored: Counter[str] = Counter()
    for t, n in bi.items():
        if n >= 2 and ok(t):
            scored[t] = n * 2
    for t, n in uni.items():
        if n >= 2 and len(t) >= 3 and ok(t) and not any(t in b.split() for b in scored):
            scored[t] = n
    return [t for t, _ in scored.most_common(limit)]
