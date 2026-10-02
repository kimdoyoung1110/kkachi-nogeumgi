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
