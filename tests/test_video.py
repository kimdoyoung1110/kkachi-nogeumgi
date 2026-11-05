from app import video


def test_video_id_from_youtube_links():
    assert video.video_id("https://youtu.be/FaZm8LHy3Bo?si=abc") == "FaZm8LHy3Bo"
    assert video.video_id("https://www.youtube.com/watch?v=FaZm8LHy3Bo&t=3") == "FaZm8LHy3Bo"
    assert video.video_id("https://www.youtube.com/shorts/FaZm8LHy3Bo") == "FaZm8LHy3Bo"


def test_make_shots_merges_flashes_and_covers_whole_video():
    shots = video.make_shots([2.92, 2.96, 5.0, 9.95], 10.0)
    assert [(s["start"], s["end"]) for s in shots] == [(0.0, 2.92), (2.92, 5.0), (5.0, 10.0)]
    assert [s["index"] for s in shots] == [1, 2, 3]


def test_make_shots_without_cuts_is_one_shot():
    assert len(video.make_shots([], 30.0)) == 1


def test_speech_goes_to_shot_where_word_is_said():
    speech = [{"words": [{"text": "멋", "start": 0.0, "end": 0.4}, {"text": "없는", "start": 0.9, "end": 1.3}]}]
    assert video.speech_in(speech, 0.0, 0.8) == "멋"
    assert video.speech_in(speech, 0.8, 2.0) == "없는"
