import numpy as np

from app import segmenter
from app.stt import attach_words, looks_repetitive, parse_language

SR = 16000


def test_parse_language_auto():
    assert parse_language("language Korean<asr_text>안녕하세요", None) == ("ko", "안녕하세요")
    assert parse_language("language English<asr_text> Hello.", None) == ("en", "Hello.")
    assert parse_language("language Japanese<asr_text>こんにちは", None) == ("japanese", "こんにちは")


def test_parse_language_no_speech():
    assert parse_language("language None<asr_text>", None) == (None, "")


def test_parse_language_forced_has_no_prefix():
    assert parse_language("안녕하세요", "ko") == ("ko", "안녕하세요")


def test_looks_repetitive():
    assert looks_repetitive("네 " * 30)
    assert looks_repetitive("감사합니다. " * 8)
    assert not looks_repetitive("오늘은 해시 테이블에 대해 배워보겠습니다. 해시 함수는 키를 인덱스로 바꿉니다.")


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
