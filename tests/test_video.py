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
    assert text.startswith("\ufeff번호,컷,") and "멋" in text


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


def test_pdf_has_all_shots_and_safe_name(tmp_path):
    from PIL import Image
    from pypdf import PdfReader

    folder = _finished(tmp_path)
    meta = video.read_json(folder / "shots.json")
    for name in meta["shots"][0]["frames"]:
        Image.new("RGB", (64, 36), "red").save(folder / name)
    meta["shots"] = [{**meta["shots"][0], "index": i + 1} for i in range(14)]
    meta["title"] = '가/나:다"'
    out = video.make_pdf(meta, folder, tmp_path / "out.pdf")
    assert len(PdfReader(out).pages) >= 2
    assert video.pdf_name(meta) == "가 나 다 샷 리스트.pdf"


def _pdf(path, pages):
    from PIL import Image

    images = [Image.new("RGB", (40, 40), "white") for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def test_join_keeps_original_and_adds_pages(tmp_path):
    from pypdf import PdfReader

    original = _pdf(tmp_path / "기획서.pdf", 2)
    before = original.read_bytes()
    out = video.join_pdfs(original, _pdf(tmp_path / "add.pdf", 3), tmp_path / "out.pdf")
    assert len(PdfReader(out).pages) == 5 and original.read_bytes() == before


def test_find_pdf_by_name_and_ambiguous_name(tmp_path):
    one = _pdf(tmp_path / "광고 기획서.pdf", 1)
    assert video.find_pdf("기획서", [tmp_path]) == one
    _pdf(tmp_path / "기획서 초안.pdf", 1)
    with pytest.raises(ValueError, match="여러 개"):
        video.find_pdf("기획서", [tmp_path])
    with pytest.raises(FileNotFoundError):
        video.find_pdf("없는 파일", [tmp_path])


def test_pick_shots_and_unknown_number(tmp_path):
    meta = video.read_json(_finished(tmp_path) / "shots.json")
    assert video.pick_shots(meta, [1])["shots"][0]["index"] == 1
    with pytest.raises(ValueError, match="없는 번호"):
        video.pick_shots(meta, [9])


def test_free_path_never_overwrites(tmp_path):
    first = _pdf(tmp_path / "a.pdf", 1)
    assert video.free_path(first).name == "a 2.pdf"


def test_instagram_links_share_one_id():
    a = video.video_id("https://www.instagram.com/reel/CDUMkliABpa/?igsh=abc")
    assert a == video.video_id("https://www.instagram.com/reels/CDUMkliABpa/") == "ig_CDUMkliABpa"
    assert video.video_id("https://www.instagram.com/someone/reel/CDUMkliABpa/") == "ig_CDUMkliABpa"


def test_fit_keeps_vertical_frame_whole():
    from PIL import Image

    tall = Image.new("RGB", (90, 160), "white")
    out = video.fit(tall, (360, 203))
    assert out.size == (360, 203)
    box = out.convert("L").getbbox()          # 흰 부분 = 원래 화면
    assert box[3] - box[1] == 203 and abs((box[2] - box[0]) / 203 - 90 / 160) < 0.02


def test_stopped_analysis_shows_as_failed(tmp_path):
    video.set_status(tmp_path / "alive", "running", "컷 나누는 중")   # 지금 이 프로세스
    video.set_status(tmp_path / "dead", "running", "컷 나누는 중", pid=2 ** 22 + 7)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "status.json").write_text(json.dumps({"state": "running", "updated_at": 1}), encoding="utf-8")
    states = {i["id"]: i["state"] for i in video.list_videos(tmp_path)}
    assert states == {"alive": "running", "dead": "failed", "old": "failed"}


def test_auto_split_cuts_long_scene_into_seconds_only():
    shots = video.make_shots([2.0], 21.0)           # 2초짜리 컷 + 19초짜리 긴 장면
    out = video.split_shots(shots, "auto")
    assert len(out) == 1 + 19
    assert out[0]["parts"] == 1 and video.label(out[0]) == "1"
    assert out[1]["cut"] == 2 and video.label(out[1]) == "2-1" and out[-1]["end"] == 21.0
    assert [s["index"] for s in out] == list(range(1, 21))


def test_fixed_and_cut_splits():
    shots = video.make_shots([], 4.0)
    assert len(video.split_shots(shots, "1")) == 4
    assert len(video.split_shots(shots, "0.5")) == 8
    assert len(video.split_shots(shots, "cut")) == 1
    with pytest.raises(ValueError):
        video.split_shots(shots, "7")


def test_split_never_explodes_on_long_video():
    out = video.split_shots(video.make_shots([], 600.0), "0.5")
    assert len(out) <= video.MAX_PIECES and out[-1]["end"] == 600.0


def test_sheets_for_split_vertical_video(tmp_path):
    from PIL import Image

    shots = video.split_shots(video.make_shots([], 3.5), "1")
    for s in shots:
        s["frames"] = [f"frames/s{s['index']}_{k}.jpg" for k in "abc"]
        for name in s["frames"]:
            (tmp_path / "frames").mkdir(exist_ok=True)
            Image.new("RGB", (90, 160), "white").save(tmp_path / name)
    sheets = video.make_sheets(tmp_path, shots)
    assert len(sheets) == 1 and Image.open(sheets[0]).height > Image.open(sheets[0]).width
