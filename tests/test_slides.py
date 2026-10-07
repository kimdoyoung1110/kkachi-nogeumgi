from app import slides
from tests.test_api import FakePipeline, WAV, make_client  # noqa: F401 (pytest fixture)


def make_pdf(pages: list[str]) -> bytes:
    """글자만 있는 아주 작은 PDF (영어, 쪽마다 한 줄씩)"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = "BT /F1 18 Tf 40 700 Td " + " ".join(f"({line}) Tj 0 -24 Td" for line in text.split("\n")) + " ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R"
                    " /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out.encode()))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out.encode())
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{x:010d} 00000 n \n" for x in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


PAGES_KO = [
    "소비자행동론 5장\n구매 의사결정 과정",
    "문제 인식\n실제 상태와 이상적 상태의 차이",
    "정보 탐색\n내부 탐색 외부 탐색 관여도",
    "구매 후 행동\n인지 부조화 cognitive dissonance 만족 불만족",
]


def utts(texts):
    return [{"id": i + 1, "text": t} for i, t in enumerate(texts)]


def test_align_follows_lecture_and_ignores_stray_sentence():
    u = utts([
        "자 오늘은 5장 구매 의사결정 과정을 볼게요",
        "먼저 문제 인식이에요. 지금 상태랑 이상적인 상태가 다를 때",
        "그 차이를 느끼면 문제 인식이 일어나요",
        "네 다음은 정보 탐색입니다",
        "내부 탐색은 기억에서 찾는 거고 외부 탐색은",
        "아 그리고 출석 부를게요",                       # 슬라이드와 상관없는 말 → 그대로 머문다
        "관여도가 높으면 외부 탐색을 많이 해요",
        "마지막으로 구매 후 행동, 인지 부조화예요",
        "cognitive dissonance 라고 하죠. 시험에 나와요",
    ])
    m = slides.align(PAGES_KO, u)
    assert [m[i] for i in range(1, 10)] == [1, 2, 2, 3, 3, 3, 3, 4, 4]


def test_align_without_text_pages():
    assert slides.align(["", ""], utts(["안녕하세요"])) == {}
    assert slides.align(PAGES_KO, []) == {}


def test_attach_slides_api(make_client, tmp_path):
    client, app = make_client(FakePipeline())
    pdf = make_pdf(["Hash Table basics\nhash function", "Collision handling\nseparate chaining collision",
                    "Linked List review"])
    with client:
        with open(WAV, "rb") as f:
            rid = client.post("/api/recordings", files={"file": ("a.wav", f, "audio/wav")},
                              data={"subject": "자료구조"}).json()["id"]
        app.state.worker.wait_idle()
        assert client.get(f"/api/recordings/{rid}").json()["slides"] is None
        bad = client.post(f"/api/recordings/{rid}/slides", files={"file": ("a.pptx", b"x")})
        assert bad.status_code == 400 and "PDF" in bad.json()["detail"]

        r = client.post(f"/api/recordings/{rid}/slides", files={"file": ("3주차.pdf", pdf, "application/pdf")})
        assert r.status_code == 200
        info = r.json()["slides"]
        assert info["name"] == "3주차.pdf" and info["pages"] == 3 and info["has_text"]
        assert "collision" in [t.lower() for t in r.json()["terms_added"]]
        # 과목 용어 힌트에도 들어감
        subj = next(s for s in client.get("/api/subjects").json() if s["name"] == "자료구조")
        assert "collision" in [t.lower() for t in subj["hotwords"]]

        d = client.get(f"/api/recordings/{rid}").json()
        assert set(d["slides"]["map"]) == {str(u["id"]) for u in d["utterances"]}
        assert client.get(f"/api/recordings/{rid}/slides.pdf").content.startswith(b"%PDF")

        hits = client.get("/api/search?q=chaining").json()["slides"]
        assert hits[0]["recording_id"] == rid and hits[0]["page"] == 2

        assert client.delete(f"/api/recordings/{rid}/slides").status_code == 204
        assert client.get(f"/api/recordings/{rid}").json()["slides"] is None
        assert client.get(f"/api/recordings/{rid}/slides.pdf").status_code == 404
