import json

import pytest

from app import video, video_connect


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


def _finished(root, vid="abc123"):
    folder = root / vid
    (folder / "frames").mkdir(parents=True)
    (folder / "frames" / "shot001_b.jpg").write_bytes(b"jpg")
    shot = {"index": 1, "start": 0.0, "end": 2.0, "length": 2.0, "text": ["멋"], "speech": "난",
            "frames": ["frames/shot001_a.jpg", "frames/shot001_b.jpg", "frames/shot001_c.jpg"]}
    meta = {"id": vid, "title": "광고", "source": "x", "path": str(folder / "video.mp4"), "duration": 2.0,
            "created_at": 1, "shots": [shot], "speech": []}
    (folder / "shots.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def test_list_shows_finished_running_and_recovers_stopped(tmp_path):
    _finished(tmp_path)
    video.set_status(tmp_path / "run1", "running", "컷 나누는 중", source="https://a.b/c")
    items = {i["id"]: i for i in video.list_videos(tmp_path)}
    assert items["abc123"]["state"] == "done" and items["abc123"]["shots"] == 1
    assert items["run1"]["state"] == "running" and items["run1"]["progress"] == 0.2
    video.recover(tmp_path)
    assert {i["id"]: i for i in video.list_videos(tmp_path)}["run1"]["state"] == "failed"


def test_failed_analysis_leaves_friendly_status(tmp_path):
    with pytest.raises(FileNotFoundError):
        video.analyze("/없는/파일.mp4", root=tmp_path, vid="bad1")
    assert video.summary(tmp_path / "bad1")["state"] == "failed"
    assert "찾지 못했어요" in video.summary(tmp_path / "bad1")["error"]


def test_csv_opens_in_excel_with_korean(tmp_path):
    folder = _finished(tmp_path)
    text = video.to_csv(video.read_json(folder / "shots.json"))
    assert text.startswith("﻿컷,") and "멋" in text


def test_load_rejects_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        video.load("../secret", root=tmp_path)


def test_connect_keeps_other_settings_and_backs_up(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"preferences": {"a": 1}, "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    assert video_connect.status("/bin/uv", tmp_path, path) == "off"
    video_connect.connect("/bin/uv", tmp_path, path)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["preferences"] == {"a": 1} and "other" in config["mcpServers"]
    assert video_connect.status("/bin/uv", tmp_path, path) == "on"
    assert "kkachi-video" not in path.with_name(path.name + ".before-kkachi").read_text(encoding="utf-8")


def test_connect_never_overwrites_broken_settings(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError):
        video_connect.connect("/bin/uv", tmp_path, path)
    assert path.read_text(encoding="utf-8") == "{ broken"


def test_connect_without_claude_app(tmp_path):
    path = tmp_path / "nope" / "claude_desktop_config.json"
    assert video_connect.status("/bin/uv", tmp_path, path) == "no_claude"
    with pytest.raises(ValueError):
        video_connect.connect("/bin/uv", tmp_path, path)
