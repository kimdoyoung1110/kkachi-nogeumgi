import numpy as np

from app import segmenter
from app.stt import attach_words, collapse_repeats, looks_repetitive, parse_language

SR = 16000


def test_parse_language_auto():
    assert parse_language("language Korean<asr_text>안녕하세요", None) == ("ko", "안녕하세요")
    assert parse_language("language English<asr_text> Hello.", None) == ("en", "Hello.")
    assert parse_language("language Japanese<asr_text>こんにちは", None) == ("japanese", "こんにちは")


def test_parse_language_no_speech():
    assert parse_language("language None<asr_text>", None) == (None, "")


def test_parse_language_forced_has_no_prefix():
    assert parse_language("안녕하세요", "ko") == ("ko", "안녕하세요")


REAL_LOOP = "이거 일회용 그거야? 그러면. " + "그냥 뭐 아니 이미지는 달라고 해야지. 이거 만든 거야? " * 40


def test_looks_repetitive():
    assert looks_repetitive("네 " * 30)
    assert looks_repetitive("감사합니다. " * 8)
    assert looks_repetitive(REAL_LOOP)  # 실제 회의에서 나온 30자 단위 루프
    assert not looks_repetitive("오늘은 해시 테이블에 대해 배워보겠습니다. 해시 함수는 키를 인덱스로 바꿉니다.")
    assert not looks_repetitive("아. 오케이 오케이 오케이. 그럼 그렇게 하자.")
    # 30초 구간에서 1,000자는 말이 안 됨
    assert looks_repetitive("가나다라마바사 아자차카타파하 " * 70, duration=30)
    natural = ("오늘은 정렬 알고리즘에 대해 이야기해 보겠습니다. 버블 정렬은 인접한 두 원소를 비교해서 바꾸는 방식이고, "
               "퀵 정렬은 피벗을 기준으로 나누는 방식입니다. 시간 복잡도는 평균적으로 n log n 이지만 최악의 경우 n 제곱이에요. "
               "그래서 실제 라이브러리는 여러 방식을 섞어서 씁니다.")
    assert not looks_repetitive(natural, duration=30)


def test_collapse_repeats():
    out = collapse_repeats(REAL_LOOP)
    assert out.count("이미지는 달라고 해야지") == 1 and out.startswith("이거 일회용")
    assert collapse_repeats("네 네 네 알겠습니다") == "네 네 네 알겠습니다"


def test_attach_words_keeps_punctuation_and_offsets():
    text = "네, 맞아요. 그걸 collision이라고 해요."
    tokens = [("네", 0.0, 0.2), ("맞아요", 0.2, 0.7), ("그걸", 0.9, 1.2),
              ("collision이라고", 1.3, 2.2), ("해요", 2.3, 2.6)]
    words = attach_words(text, tokens, offset=10.0)
    assert [w.text for w in words] == ["네,", "맞아요.", "그걸", "collision이라고", "해요."]
    assert words[0].start == 10.0 and words[-1].end == 12.6


def test_attach_words_fills_missing_times():
    words = attach_words("가 나 다", [("가", 0.0, 0.5), ("다", 1.0, 1.5)], offset=0.0)
    assert words[1].start == 0.5 and words[1].end == 1.0


def test_split_respects_max_and_cuts_at_silence():
    tone = 0.1 * np.sin(np.linspace(0, 2000, SR * 25)).astype(np.float32)
    silence = np.zeros(SR * 1, dtype=np.float32)
    audio = np.concatenate([tone, silence, tone, silence, tone])  # 약 77초
    spans = segmenter.split(audio, SR, max_seconds=30)
    assert spans[0].start == 0 and spans[-1].end == len(audio)
    assert all((s.end - s.start) / SR <= 30 for s in spans)
    # 첫 컷은 25~26초 사이 무음 구간
    assert 25 <= spans[0].end / SR <= 26
    assert all(a.end == b.start for a, b in zip(spans, spans[1:]))


def test_is_silent():
    assert segmenter.is_silent(np.zeros(SR, dtype=np.float32))
    assert not segmenter.is_silent(0.1 * np.ones(SR, dtype=np.float32))


def test_word_language():
    from app.stt import word_language
    assert word_language("collision이라고", "en") == "ko"
    assert word_language("quicksort,", "ko") == "en"
    assert word_language("123", "ko") == "ko"


def test_apply_replacements_longest_first():
    from app.stt import apply_replacements
    rep = {"체인잉": "chaining", "세프리 체인잉": "separate chaining", "컬리전": "collision"}
    assert apply_replacements("컬리전이라고 하고 세프리 체인잉이랑 체인잉", rep) == \
        "collision이라고 하고 separate chaining이랑 chaining"
    assert apply_replacements("그대로", None) == "그대로"


def _probs(spans, n_frames=1000, n_spk=4, frame=0.01):
    p = np.zeros((n_frames, n_spk), dtype=np.float32)
    for s, e, k in spans:
        p[int(s / frame):int(e / frame), k] = 0.9
    return p


def test_turns_merge_short_gaps_and_blips():
    probs = _probs([(0, 2, 1), (2.2, 4, 1), (4.0, 4.1, 2), (4.5, 6, 0)])
    turns = segmenter.turns_from_probs(probs, 0.01)
    assert [(round(t.start, 1), round(t.end, 1), t.speaker) for t in turns] == [(0, 4.1, 1), (4.5, 6, 0)]


def test_split_by_turns_prefers_speaker_changes_and_carves_long_gaps():
    audio = np.zeros(SR * 80, dtype=np.float32)
    turns = [segmenter.Turn(0, 12, 0), segmenter.Turn(12.4, 25, 1), segmenter.Turn(25.2, 40, 0),
             segmenter.Turn(45, 50, 1)]
    spans = segmenter.split_by_turns(audio, turns, SR, max_seconds=30)
    secs = [(round(s.start / SR, 1), round(s.end / SR, 1)) for s in spans]
    # 0~25.1: 30초 안의 마지막 화자 교체 지점(25.1)에서 자름 | 25.1~40.2 | 40.2~44.8 공백 | 44.8~80
    assert secs[:3] == [(0.0, 25.1), (25.1, 40.2), (40.2, 44.8)]
    assert secs[3][0] == 44.8 and secs[-1][1] == 80.0
    assert all((s.end - s.start) / SR <= 30 for s in spans)


def test_fill_unknown_languages():
    from app.langid import fill_unknown
    assert fill_unknown([None, "en", None, "ko", None]) == ["en", "en", "en", "ko", "ko"]
    assert fill_unknown([None, None]) == ["ko", "ko"]


def test_language_regions_cover_audio_and_cut_between_pieces():
    P, S = segmenter.Piece, segmenter.Span
    pieces = [P(S(100, 200), 0), P(S(250, 300), 0), P(S(400, 500), 1), P(S(520, 600), 0)]
    regions = segmenter.language_regions(pieces, ["ko", "ko", "en", "ko"], total_samples=700)
    assert regions == [(0, 350, "ko"), (350, 510, "en"), (510, 700, "ko")]


def test_pieces_from_turns_splits_long_turn():
    audio = (0.1 * np.ones(SR * 20)).astype(np.float32)
    pieces = segmenter.pieces_from_turns(audio, [segmenter.Turn(0, 20, 3)], SR, max_seconds=8)
    assert len(pieces) >= 3 and all(p.speaker == 3 for p in pieces)
    assert all((p.span.end - p.span.start) / SR <= 8 for p in pieces)


def test_decide_drops_short_unsure_minority_language():
    from app.langid import decide
    # 한국어 회의 (주 언어 ko). 짧은 웃음 조각이 en 으로 잡혀도 ko 로 바꾼다
    d = [8, 8, 2.5, 8, 8, 8, 8, 8, 8, 8]
    s = [{"ko": .99, "en": .01}] * 10
    s = list(s)
    s[2] = {"ko": .1, "en": .9}       # 짧고 덜 확실한 영어 → 한국어로
    s[5] = {"ko": .01, "en": .99}     # 길고 아주 확실한 영어 → 인정
    labels, dom = decide(d, s)
    assert dom == "ko" and labels[2] == "ko" and labels[5] == "en"


def test_decide_keeps_mixed_lecture():
    from app.langid import decide
    # 한·영이 섞인 강의 (주 언어 비율 < 80%) 는 70% 기준 그대로
    d = [8] * 6
    s = [{"ko": .9, "en": .1}, {"ko": .2, "en": .8}] * 3
    labels, _ = decide(d, s)
    assert labels == ["ko", "en"] * 3


def test_decide_unknown_follows_neighbors():
    from app.langid import decide
    labels, dom = decide([1.0, 8, 8], [None, {"ko": .1, "en": .9}, {"ko": .1, "en": .9}])
    assert labels == ["en", "en", "en"] and dom == "en"
