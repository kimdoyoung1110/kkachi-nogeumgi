import numpy as np

from app.diarize import SpeakerActivity
from app.merge import assign_speakers, build_utterances
from app.stt import Segment, Word

FRAME = 0.01


def activity(spans, n_frames=1000, n_spk=8):
    """spans: [(start, end, speaker)] 초 단위로 해당 화자가 말한 구간."""
    probs = np.zeros((n_frames, n_spk), dtype=np.float32)
    for s, e, spk in spans:
        probs[int(s / FRAME):int(e / FRAME), spk] = 0.9
    return SpeakerActivity(probs, FRAME)


def seg(words, lang="ko"):
    ws = [Word(t, s, e) for t, s, e in words]
    return Segment(ws[0].start, ws[-1].end, lang, " ".join(w.text for w in ws), ws)


def test_assign_and_group_by_speaker():
    act = activity([(0, 2, 5), (2.5, 4, 3)])  # 모델 내부 번호는 5, 3
    segs = [seg([("안녕하세요.", 0.1, 0.9), ("여러분", 1.0, 1.8), ("질문", 2.6, 3.0), ("있어요?", 3.1, 3.8)])]
    utts = build_utterances(assign_speakers(segs, act))
    assert [(u.speaker, u.text) for u in utts] == [(0, "안녕하세요. 여러분"), (1, "질문 있어요?")]


def test_short_blip_is_smoothed():
    act = activity([(0, 1.0, 0), (1.0, 1.3, 1), (1.3, 3, 0)])
    segs = [seg([("가", 0.1, 0.9), ("나", 1.05, 1.25), ("다", 1.4, 2.0)])]
    words = assign_speakers(segs, act)
    assert {w.speaker for w in words} == {0}


def test_word_without_activity_follows_previous_speaker():
    act = activity([(0, 1, 2)])
    segs = [seg([("가", 0.1, 0.9), ("나", 5.0, 5.5)])]
    assert [w.speaker for w in assign_speakers(segs, act)] == [0, 0]


def test_num_speakers_limits_to_most_active():
    act = activity([(0, 3, 0), (3, 3.5, 1), (3.5, 6, 2)])
    act.probs[300:350, 0] = 0.3  # 1번 화자 구간에 0번 화자 흔적
    segs = [seg([("가", 0.5, 2.5), ("나", 3.05, 3.45), ("다", 4.0, 5.5)])]
    words = assign_speakers(segs, act, num_speakers=2)
    assert len({w.speaker for w in words}) == 2


def test_paragraph_breaks_on_long_pause_after_sentence():
    act = activity([(0, 10, 0)])
    segs = [seg([("첫 문장.", 0.0, 1.0), ("둘째", 4.0, 4.5), ("문장", 4.5, 5.0)])]
    utts = build_utterances(assign_speakers(segs, act))
    assert [u.text for u in utts] == ["첫 문장.", "둘째 문장"]
    assert all(u.speaker == 0 for u in utts)


def test_utterance_language_is_majority():
    act = activity([(0, 5, 0)])
    segs = [seg([("a", 0, 1), ("b", 1, 2)], "en"), seg([("가", 2, 3)], "ko")]
    assert build_utterances(assign_speakers(segs, act))[0].language == "en"
