from app import export

REC = {"title": "자료구조 3주차", "created_at": 1790000000.0, "duration": 125.0, "subject": "자료구조"}
TR = {
    "speakers": [{"idx": 0, "name": "교수님"}, {"idx": 1, "name": "화자 2"}],
    "utterances": [
        {"speaker": 0, "start": 0.0, "end": 3.0, "text": "해시 테이블을 배워요.",
         "words": [["해시", 0.0, 0.5], ["테이블을", 0.5, 1.2], ["배워요.", 1.2, 3.0]]},
        {"speaker": 1, "start": 65.5, "end": 70.0, "text": "Is that a collision?", "words": []},
        {"speaker": 1, "start": 71.0, "end": 72.0, "text": "Thanks.", "words": []},
    ],
}


def test_duration_text():
    assert export._duration(39) == "39초"
    assert export._duration(125) == "2분 5초"
    assert export._duration(7300) == "2시간 1분"


def test_clock():
    assert export.clock(65.5) == "01:05"
    assert export.clock(3725.25, millis=True) == "01:02:05,250"


def test_txt():
    out = export.to_txt(REC, TR)
    assert out.startswith("자료구조 3주차\n")
    assert "2분 5초" in out
    assert "[00:00] 교수님\n해시 테이블을 배워요.\n\n[01:05] 화자 2\nIs that a collision?" in out


def test_md():
    out = export.to_md(REC, TR)
    assert out.startswith("# 자료구조 3주차\n")
    assert "과목: 자료구조" in out
    assert "**교수님** `00:00`\n\n해시 테이블을 배워요." in out


def test_srt_speaker_prefix_only_on_change():
    out = export.to_srt(REC, TR)
    blocks = out.strip().split("\n\n")
    assert blocks[0] == "1\n00:00:00,000 --> 00:00:03,000\n교수님: 해시 테이블을 배워요."
    assert blocks[1] == "2\n00:01:05,500 --> 00:01:10,000\n화자 2: Is that a collision?"
    assert blocks[2] == "3\n00:01:11,000 --> 00:01:12,000\nThanks."


def test_srt_splits_long_utterance_by_words():
    words = [[f"단어{i}", i * 1.0, i * 1.0 + 0.9] for i in range(20)]
    tr = {"speakers": [{"idx": 0, "name": "교수님"}],
          "utterances": [{"speaker": 0, "start": 0, "end": 20, "text": " ".join(w[0] for w in words), "words": words}]}
    cues = export._cues(tr["utterances"][0])
    assert len(cues) > 2
    assert all(e - s <= export.SRT_MAX_SECONDS + 1 and len(t) <= export.SRT_MAX_CHARS for s, e, t in cues)
    assert " ".join(t for _, _, t in cues) == tr["utterances"][0]["text"]
