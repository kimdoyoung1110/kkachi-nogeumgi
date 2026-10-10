import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, parse_replacements, parse_terms
from app.merge import SpeakerWord, Utterance

WAV = "tests/audio/mixed_ko_en.wav"


class FakePipeline:
    def __init__(self, fail=False, gate=None):
        self.fail = fail
        self.gate = gate
        self.calls = []
        self.retranscribe_calls = []

    def retranscribe(self, audio, start, end, language, hotwords=None, replacements=None):
        from app.stt import Segment, Word
        self.retranscribe_calls.append(dict(start=start, end=end, language=language))
        if self.fail:
            raise RuntimeError("boom")
        return [Segment(start, end, language or "en", "Is that a collision?",
                        [Word("Is", start, start + 0.1), Word("that", start + 0.1, start + 0.2),
                         Word("a", start + 0.2, start + 0.3), Word("collision?", start + 0.3, end)])]

    def process(self, audio, language=None, hotwords=None, num_speakers=None, replacements=None, progress=None):
        self.calls.append(dict(language=language, hotwords=hotwords, num_speakers=num_speakers,
                               replacements=replacements))
        if self.gate:
            self.gate.wait(5)
        for stage in ("diarize", "langid", "transcribe", "align"):
            progress(stage, 1, 1)
        if self.fail:
            raise RuntimeError("boom")
        w = [SpeakerWord("해시", 0.0, 0.5, 0, "ko"), SpeakerWord("테이블입니다.", 0.5, 1.0, 0, "ko")]
        return [
            Utterance(0, 0.0, 1.0, "ko", "해시 테이블입니다.", w),
            Utterance(1, 1.5, 2.0, "en", "Is that a collision?", [SpeakerWord("Is", 1.5, 1.6, 1, "en")]),
        ]


@pytest.fixture
def make_client(tmp_path):
    def make(pipeline):
        app = create_app(data_dir=tmp_path, pipeline_factory=lambda: pipeline)
        return TestClient(app), app
    return make


def upload(client, **form):
    with open(WAV, "rb") as f:
        return client.post("/api/recordings", files={"file": ("강의 1.wav", f, "audio/wav")}, data=form)


def test_upload_process_and_fetch(make_client, tmp_path):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    with client:
        r = upload(client, language="ko", num_speakers="2", hotwords="hash table, collision",
                   replacements="컬리전=collision\n세프리 체인잉 → separate chaining")
        assert r.status_code == 201
        rec = r.json()
        # 처리기가 빠르면 응답 전에 이미 처리 중일 수 있다 (예전에 가끔 실패하던 원인)
        assert rec["title"] == "강의 1" and rec["status"] in ("queued", "processing", "done")
        assert app.state.worker.wait_idle()

        d = client.get(f"/api/recordings/{rec['id']}").json()
        assert d["status"] == "done" and d["progress"] == 1.0 and d["stage_label"] == "완료"
        assert d["finished_at"] > 0
        assert d["duration"] > 20
        assert [u["text"] for u in d["utterances"]] == ["해시 테이블입니다.", "Is that a collision?"]
        assert d["utterances"][0]["words"][0] == ["해시", 0.0, 0.5]
        assert d["speakers"] == [{"idx": 0, "name": "화자 1"}, {"idx": 1, "name": "화자 2"}]
        assert pipe.calls[0] == dict(language="ko", hotwords=["hash table", "collision"], num_speakers=2,
                                     replacements={"컬리전": "collision", "세프리 체인잉": "separate chaining"})
        assert client.get(f"/api/recordings/{rec['id']}/audio").status_code == 200
        assert (tmp_path / "recordings" / rec["id"] / "original.wav").exists()


def test_auto_language_passes_none(make_client):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    with client:
        upload(client)
        app.state.worker.wait_idle()
    assert pipe.calls[0]["language"] is None and pipe.calls[0]["num_speakers"] is None


def test_failure_keeps_file_and_retry_works(make_client, tmp_path):
    pipe = FakePipeline(fail=True)
    client, app = make_client(pipe)
    with client:
        rec = upload(client).json()
        app.state.worker.wait_idle()
        d = client.get(f"/api/recordings/{rec['id']}/status").json()
        assert d["status"] == "failed" and "다시 시도" in d["error"]
        assert (tmp_path / "recordings" / rec["id"] / "original.wav").exists()

        pipe.fail = False
        assert client.post(f"/api/recordings/{rec['id']}/retry").status_code == 200
        app.state.worker.wait_idle()
        assert client.get(f"/api/recordings/{rec['id']}/status").json()["status"] == "done"


def test_undecodable_file_gets_friendly_error(make_client):
    client, app = make_client(FakePipeline())
    with client:
        rec = client.post("/api/recordings", files={"file": ("x.m4a", b"not audio", "audio/mp4")}).json()
        app.state.worker.wait_idle()
        d = client.get(f"/api/recordings/{rec['id']}").json()
        assert d["status"] == "failed" and "소리를 읽을 수 없어요" in d["error"]


def test_interrupted_job_marked_failed_on_restart(make_client, tmp_path):
    gate = threading.Event()
    client, app = make_client(FakePipeline(gate=gate))
    with client:
        rec = upload(client).json()
        # 처리 중인 상태를 흉내: 워커가 gate 에서 기다리는 동안 DB 를 processing 으로 둔 채 앱을 '재시작'
        app.state.db.update_recording(rec["id"], status="processing")
        gate.set()
        app.state.worker.wait_idle()
    app.state.db.update_recording(rec["id"], status="processing")  # 꺼질 때 처리 중이었다고 가정

    client2, app2 = make_client(FakePipeline())
    with client2:
        d = client2.get(f"/api/recordings/{rec['id']}/status").json()
        assert d["status"] == "failed" and "앱이 꺼져서" in d["error"]


def test_queued_jobs_resume_on_restart(make_client):
    client, app = make_client(FakePipeline())
    rec = app.state.db.create_recording("대기", "original.wav")  # 서버 시작 전 큐에 있던 작업
    import shutil
    dest = app.state.worker.recordings_dir / rec["id"]
    dest.mkdir(parents=True)
    shutil.copy(WAV, dest / "original.wav")
    with client:
        app.state.worker.wait_idle()
        assert client.get(f"/api/recordings/{rec['id']}/status").json()["status"] == "done"


def test_delete_removes_files(make_client, tmp_path):
    client, app = make_client(FakePipeline())
    with client:
        rec = upload(client).json()
        app.state.worker.wait_idle()
        assert client.delete(f"/api/recordings/{rec['id']}").status_code == 204
        assert client.get(f"/api/recordings/{rec['id']}").status_code == 404
        assert not (tmp_path / "recordings" / rec["id"]).exists()
        assert client.get("/api/recordings").json() == []


def test_validation(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        assert upload(client, language="fr").status_code == 400
        assert upload(client, num_speakers="12").status_code == 400


def test_fts_search_korean_substring(make_client):
    client, app = make_client(FakePipeline())
    with client:
        upload(client)
        app.state.worker.wait_idle()
    with app.state.db.conn() as c:
        rows = c.execute("SELECT text FROM utterances_fts WHERE utterances_fts MATCH '테이블'").fetchall()
    assert [r[0] for r in rows] == ["해시 테이블입니다."]


def test_parsers():
    assert parse_terms(" a, b\nc ,") == ["a", "b", "c"]
    assert parse_replacements("x=y\n가 -> 나\n잘못된줄") == {"x": "y", "가": "나"}


def test_subject_terms_saved_on_upload(make_client):
    client, app = make_client(FakePipeline())
    with client:
        upload(client, subject="자료구조", hotwords="hash table", replacements="컬리전=collision")
        upload(client, subject="운영체제", hotwords="semaphore")
        app.state.worker.wait_idle()
        subs = client.get("/api/subjects").json()
        assert [x["name"] for x in subs] == ["운영체제", "자료구조"]
        assert subs[1] == {"name": "자료구조", "hotwords": ["hash table"], "replacements": {"컬리전": "collision"}}
        assert client.delete("/api/subjects/운영체제").status_code == 204
        assert [x["name"] for x in client.get("/api/subjects").json()] == ["자료구조"]


# ---- 브라우저 녹음 ----

def _webm_chunks(tmp_path, n=3):
    """진짜 webm 녹음을 만들어 MediaRecorder 처럼 바이트 단위로 n 조각으로 나눈다."""
    import subprocess, imageio_ffmpeg
    out = tmp_path / "src.webm"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-i", WAV,
                    "-c:a", "libopus", "-b:a", "48k", str(out)], check=True)
    data = out.read_bytes()
    size = len(data) // n + 1
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_live_recording_flow(make_client, tmp_path):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    with client:
        rec = client.post("/api/live", data={"title": "실시간", "container": "webm"}).json()
        assert rec["status"] == "recording" and rec["stage_label"] == "녹음 중" and rec["stalled"] is False
        rid = rec["id"]

        chunks = _webm_chunks(tmp_path)
        # 순서가 섞여 오거나 같은 조각이 다시 와도 결과는 같아야 한다
        for seq in (1, 0, 2, 1):
            assert client.put(f"/api/live/{rid}/chunks/{seq}", content=chunks[seq]).status_code == 204

        assert client.post(f"/api/recordings/{rid}/retry").status_code == 409  # 녹음 중엔 불가
        r = client.post(f"/api/live/{rid}/finish", data={"title": "자료구조 녹음",
                                                         "language": "ko", "subject": "자료구조"})
        assert r.status_code == 200 and r.json()["status"] in ("queued", "processing", "done")
        app.state.worker.wait_idle()

        d = client.get(f"/api/recordings/{rid}").json()
        assert d["status"] == "done" and d["title"] == "자료구조 녹음" and d["source"] == "record"
        assert d["file_name"] == "original.webm" and 25 < d["duration"] < 30
        assert pipe.calls[-1]["language"] == "ko"
        rec_dir = tmp_path / "recordings" / rid
        assert not (rec_dir / "chunks").exists()
        assert client.get(f"/api/recordings/{rid}/audio").status_code == 200

        # 끝난 녹음엔 더 못 붙임
        assert client.put(f"/api/live/{rid}/chunks/3", content=b"x").status_code == 409
        assert client.post(f"/api/live/{rid}/finish").status_code == 409


def test_live_app_audio_pcm_tracks(make_client, tmp_path):
    """앱 창의 '다른 앱 소리 녹음': 실행기가 앱 소리·마이크 PCM 을 따로 보내면 섞어서 m4a 로 만든다."""
    import math, struct
    pipe = FakePipeline()
    client, app = make_client(pipe)

    def tone(freq, sec):
        n = 16000 * sec
        return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n)))

    with client:
        rec = client.post("/api/live", data={"title": "줌 수업", "container": "pcm"}).json()
        rid = rec["id"]
        for seq in (1, 0, 2):  # 10초 조각 대신 2초짜리 3개, 순서 섞어서
            assert client.put(f"/api/live/{rid}/chunks/{seq}?track=app", content=tone(440, 2)).status_code == 204
        assert client.put(f"/api/live/{rid}/chunks/0?track=mic", content=tone(220, 2)).status_code == 204
        assert client.put(f"/api/live/{rid}/chunks/0?track=evil", content=b"xx").status_code == 400
        assert client.get(f"/api/recordings/{rid}").json()["stalled"] is False

        r = client.post(f"/api/live/{rid}/finish", data={"language": "ko"})
        assert r.status_code == 200
        app.state.worker.wait_idle()
        d = client.get(f"/api/recordings/{rid}").json()
        assert d["status"] == "done" and d["file_name"] == "original.m4a"
        assert 5.5 < d["duration"] < 6.5  # 더 긴 트랙(앱 소리 6초) 기준
        assert not (tmp_path / "recordings" / rid / "chunks").exists()


def test_live_finish_without_chunks_fails(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        rid = client.post("/api/live").json()["id"]
        r = client.post(f"/api/live/{rid}/finish")
        assert r.status_code == 400 and "조각" in r.json()["detail"]


def test_live_stalled_detection(make_client, tmp_path):
    import os, time
    from app import live
    client, _ = make_client(FakePipeline())
    with client:
        rid = client.post("/api/live").json()["id"]
        client.put(f"/api/live/{rid}/chunks/0", content=b"abc")
        old = time.time() - live.STALLED_SECONDS - 5
        for p in (tmp_path / "recordings" / rid).rglob("*"):
            os.utime(p, (old, old))
        os.utime(tmp_path / "recordings" / rid, (old, old))
        items = client.get("/api/recordings").json()
        assert items[0]["status"] == "recording" and items[0]["stalled"] is True


def test_recording_status_survives_restart(make_client):
    client, app = make_client(FakePipeline())
    with client:
        rid = client.post("/api/live").json()["id"]
    client2, _ = make_client(FakePipeline())
    with client2:
        # 녹음 중이던 건 실패로 바꾸지 않는다 (저장된 부분을 나중에 받아쓸 수 있게)
        assert client2.get(f"/api/recordings/{rid}/status").json()["status"] == "recording"


# ---- 결과 편집 ----

def _done_recording(client, app):
    rec = upload(client).json()
    app.state.worker.wait_idle()
    return client.get(f"/api/recordings/{rec['id']}").json()


def test_rename_recording_and_speaker(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        rid = d["id"]
        assert client.patch(f"/api/recordings/{rid}", json={"title": "  새 제목 "}).json()["title"] == "새 제목"
        assert client.patch(f"/api/recordings/{rid}", json={"title": " "}).status_code == 400
        assert client.patch(f"/api/recordings/{rid}/speakers/0", json={"name": "교수님"}).status_code == 200
        new = client.post(f"/api/recordings/{rid}/speakers").json()
        assert new == {"idx": 2, "name": "화자 3"}
        speakers = client.get(f"/api/recordings/{rid}").json()["speakers"]
        assert [s["name"] for s in speakers] == ["교수님", "화자 2", "화자 3"]


def test_edit_utterance_text_and_speaker(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        u = d["utterances"][0]
        r = client.patch(f"/api/utterances/{u['id']}", json={"text": "해시 테이블이에요."}).json()
        assert r["text"] == "해시 테이블이에요." and r["words"] == [] and r["start"] == u["start"]
        assert client.patch(f"/api/utterances/{u['id']}", json={"speaker": 1}).json()["speaker"] == 1
        assert client.patch(f"/api/utterances/{u['id']}", json={"speaker": 7}).status_code == 400
        assert client.patch(f"/api/utterances/{u['id']}", json={"text": "  "}).status_code == 400
        # 고친 내용이 검색에도 반영된다
        with app.state.db.conn() as c:
            hits = c.execute("SELECT text FROM utterances_fts WHERE utterances_fts MATCH '테이블이에요'").fetchall()
        assert len(hits) == 1


def test_delete_utterance(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        assert client.delete(f"/api/utterances/{d['utterances'][0]['id']}").status_code == 204
        assert len(client.get(f"/api/recordings/{d['id']}").json()["utterances"]) == 1
        assert client.delete("/api/utterances/99999").status_code == 404


def test_retranscribe_utterance(make_client):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    with client:
        d = _done_recording(client, app)
        u = d["utterances"][0]
        r = client.post(f"/api/utterances/{u['id']}/retranscribe", json={"language": "en"})
        assert r.status_code == 202
        app.state.worker.wait_idle()
        after = client.get(f"/api/recordings/{d['id']}").json()["utterances"][0]
        assert after["text"] == "Is that a collision?" and after["language"] == "en" and not after["busy"]
        assert len(after["words"]) == 4 and after["speaker"] == u["speaker"]
        assert pipe.retranscribe_calls[-1] == dict(start=u["start"], end=u["end"], language="en")
        assert client.post(f"/api/utterances/{u['id']}/retranscribe", json={"language": "fr"}).status_code == 400


def test_retranscribe_failure_keeps_text(make_client):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    with client:
        d = _done_recording(client, app)
        u = d["utterances"][0]
        pipe.fail = True
        client.post(f"/api/utterances/{u['id']}/retranscribe", json={"language": "ko"})
        app.state.worker.wait_idle()
        after = client.get(f"/api/recordings/{d['id']}").json()["utterances"][0]
        assert after["text"] == u["text"] and not after["busy"]


def test_busy_flags_cleared_on_restart(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
    app.state.db.update_utterance(d["utterances"][0]["id"], busy=1)
    client2, _ = make_client(FakePipeline())
    with client2:
        assert client2.get(f"/api/recordings/{d['id']}").json()["utterances"][0]["busy"] is False


# ---- 검색 / 내보내기 ----

def test_search(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        client.patch(f"/api/recordings/{d['id']}/speakers/1", json={"name": "학생"})
        r = client.get("/api/search", params={"q": "테이블"}).json()
        assert r["count"] == 1 and r["results"][0]["text"] == "해시 테이블입니다."
        assert r["results"][0]["title"] == "강의 1" and r["results"][0]["speaker_name"] == "화자 1"
        # 영문 대소문자 무시, 3글자 이상(색인)
        hit = client.get("/api/search", params={"q": "COLLISION"}).json()["results"][0]
        assert hit["speaker_name"] == "학생" and hit["start"] == 1.5
        # 2글자 이하는 LIKE 로 찾음
        assert client.get("/api/search", params={"q": "해시"}).json()["count"] == 1
        # 특수문자도 글자로 취급 (오류 없이 0건)
        for q in ['"', "%", "_", "a OR b", "NEAR(", "*"]:
            assert client.get("/api/search", params={"q": q}).status_code == 200
        assert client.get("/api/search", params={"q": "%"}).json()["count"] == 0
        assert client.get("/api/search", params={"q": "  "}).json()["count"] == 0


def test_export_endpoints(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        r = client.get(f"/api/recordings/{d['id']}/export", params={"format": "srt"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-subrip")
        assert "filename*=UTF-8''%EA%B0%95%EC%9D%98%201.srt" in r.headers["content-disposition"]
        assert "화자 1: 해시 테이블입니다." in r.text
        r = client.get(f"/api/recordings/{d['id']}/export", params={"format": "txt", "download": "false"})
        assert "content-disposition" not in r.headers and "[00:00] 화자 1" in r.text
        assert client.get(f"/api/recordings/{d['id']}/export", params={"format": "pdf"}).status_code == 400


# ---- 앱 관리 ----

def test_app_info_and_busy_blocks_update(make_client, monkeypatch):
    from app import system
    client, app = make_client(FakePipeline())
    with client:
        info = client.get("/api/app").json()
        assert info["busy"] == [] and "commit" in info["version"]
        client.post("/api/live")  # 녹음 중
        assert client.get("/api/app").json()["busy"] == ["녹음 중이에요"]
        called = []
        monkeypatch.setattr(system, "apply_update", lambda *a: called.append(1))
        r = client.post("/api/app/update")
        assert r.status_code == 409 and "녹음 중" in r.json()["detail"] and not called


def test_quit_and_logs(make_client, monkeypatch, tmp_path):
    from app import system
    client, app = make_client(FakePipeline())
    quits = []
    monkeypatch.setattr(system, "shutdown_soon", lambda: quits.append(1))
    app.state.log_export_dir = tmp_path / "desk"
    app.state.reveal_files = False
    with client:
        assert client.get("/api/health").json()["app"] == "kkachi"
        assert client.post("/api/app/quit").json() == {"ok": True} and quits == [1]
        r = client.post("/api/app/logs").json()
        assert r["name"].startswith("까치녹음기-로그-") and (tmp_path / "desk" / r["name"]).exists()


def test_merge_speaker_joins_adjacent_paragraphs(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        rid = d["id"]
        # 화자 0 문단 뒤에 화자 1 문단 → 1을 0으로 합치면 문단 하나가 된다
        r = client.post(f"/api/recordings/{rid}/speakers/1/merge", json={"into": 0})
        assert r.status_code == 200
        t = r.json()
        assert [s["idx"] for s in t["speakers"]] == [0]
        assert len(t["utterances"]) == 1
        u = t["utterances"][0]
        assert u["text"] == "해시 테이블입니다. Is that a collision?" and u["end"] == 2.0 and len(u["words"]) == 3
        assert client.post(f"/api/recordings/{rid}/speakers/0/merge", json={"into": 0}).status_code == 400
        assert client.post(f"/api/recordings/{rid}/speakers/5/merge", json={"into": 0}).status_code == 400


def test_change_category(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        rid = d["id"]
        r = client.patch(f"/api/recordings/{rid}", json={"subject": " 팀 회의 "}).json()
        assert r["subject"] == "팀 회의" and r["title"] == "강의 1"
        assert "팀 회의" in [x["name"] for x in client.get("/api/subjects").json()]
        assert client.patch(f"/api/recordings/{rid}", json={"subject": ""}).json()["subject"] is None
        assert client.patch(f"/api/recordings/{rid}", json={"title": "새 제목"}).json()["subject"] is None


# ---- 중요 표시 / 전원 / 선물 ----

def test_marks_survive_redo(make_client):
    client, app = make_client(FakePipeline())
    with client:
        d = _done_recording(client, app)
        rid = d["id"]
        assert client.post(f"/api/recordings/{rid}/marks", json={"t": 0.4}).json()["marks"] == [0.4]
        client.post(f"/api/recordings/{rid}/marks", json={"t": 1.7})
        assert client.get(f"/api/recordings/{rid}").json()["marks"] == [0.4, 1.7]
        # 다시 받아써도 시간 기준 표시는 그대로
        client.post(f"/api/recordings/{rid}/retry")
        app.state.worker.wait_idle()
        assert client.get(f"/api/recordings/{rid}").json()["marks"] == [0.4, 1.7]
        r = client.delete(f"/api/recordings/{rid}/marks", params={"start": 1.5, "end": 2.0}).json()
        assert r["marks"] == [0.4]
        assert client.post(f"/api/recordings/{rid}/marks", json={"t": -1}).status_code == 400


def test_marks_while_recording(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        rid = client.post("/api/live").json()["id"]
        assert client.post(f"/api/recordings/{rid}/marks", json={"t": 12.5}).status_code == 201


def test_gift_config(make_client, tmp_path):
    import json
    client, _ = make_client(FakePipeline())
    with client:
        assert client.get("/api/app/gift").json() == {}
        (tmp_path / "gift.json").write_text(json.dumps({"name": " 지은 ", "letter": "예전 버전 편지", "x": 1}))
        assert client.get("/api/app/gift").json() == {"name": "지은"}
        (tmp_path / "gift.json").write_text("{깨진 파일")
        assert client.get("/api/app/gift").json() == {}


def test_notes_during_recording(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        rid = client.post("/api/live").json()["id"]
        r = client.post(f"/api/recordings/{rid}/notes", json={"t": 12.5, "text": "  시험에 나옴  "})
        assert r.status_code == 201 and r.json()["notes"][0]["text"] == "시험에 나옴"
        client.post(f"/api/recordings/{rid}/notes", json={"t": 3, "text": "과제 공지"})
        notes = client.get(f"/api/recordings/{rid}").json()["notes"]
        assert [n["text"] for n in notes] == ["과제 공지", "시험에 나옴"]  # 시간순
        assert client.post(f"/api/recordings/{rid}/notes", json={"t": 1, "text": "  "}).status_code == 400
        nid = notes[0]["id"]
        assert client.patch(f"/api/recordings/{rid}/notes/{nid}", json={"text": "과제: 금요일까지"}).json()["notes"][0]["text"] == "과제: 금요일까지"
        assert len(client.delete(f"/api/recordings/{rid}/notes/{nid}").json()["notes"]) == 1
        assert client.delete(f"/api/recordings/{rid}/notes/{nid}").status_code == 404


def test_notes_in_export(make_client):
    client, app = make_client(FakePipeline())
    with client:
        with open(WAV, "rb") as f:
            rid = client.post("/api/recordings", files={"file": ("a.wav", f, "audio/wav")}).json()["id"]
        app.state.worker.wait_idle()
        client.post(f"/api/recordings/{rid}/notes", json={"t": 0.5, "text": "중요한 공식"})
        txt = client.get(f"/api/recordings/{rid}/export?format=txt").text
        md = client.get(f"/api/recordings/{rid}/export?format=md").text
        assert "📝 메모: 중요한 공식" in txt and "> 📝 중요한 공식" in md


def test_extract_terms_from_pptx(make_client):
    import io, zipfile
    slide = ('<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>인지 부조화(cognitive dissonance)</a:t></a:r></a:p>'
             '<a:p><a:r><a:t>Customer Lifetime Value와 CRM, </a:t></a:r><a:r><a:t>CRM 전략</a:t></a:r></a:p></p:sld>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ppt/slides/slide1.xml", slide)
    client, _ = make_client(FakePipeline())
    with client:
        r = client.post("/api/terms/extract", files={"file": ("ch5.pptx", buf.getvalue())})
        assert r.status_code == 200
        found = r.json()["terms"]
        assert "cognitive dissonance" in found and "인지 부조화" in found and "CRM" in found
        assert "Customer Lifetime Value" in found
        bad = client.post("/api/terms/extract", files={"file": ("a.hwp", b"xx")})
        assert bad.status_code == 400 and "PDF" in bad.json()["detail"]
        empty = client.post("/api/terms/extract", files={"file": ("a.txt", b"hi")})
        assert empty.status_code == 400


def _fake_voicememos(root):
    import shutil, sqlite3
    rec = root / "Recordings"
    rec.mkdir(parents=True)
    shutil.copy(WAV, rec / "20261005 103000-AAAA.m4a")
    shutil.copy(WAV, rec / "20261006 090000-BBBB.m4a")
    c = sqlite3.connect(rec / "CloudRecordings.db")
    c.execute("CREATE TABLE ZCLOUDRECORDING (Z_PK INTEGER, ZPATH TEXT, ZENCRYPTEDTITLE TEXT, ZDATE REAL,"
              " ZDURATION REAL, ZUNIQUEID TEXT)")
    c.execute("INSERT INTO ZCLOUDRECORDING VALUES (1, '20261005 103000-AAAA.m4a', '소비자행동 5주차', 812000000, 3.2, 'AAAA')")
    c.execute("INSERT INTO ZCLOUDRECORDING VALUES (2, '20261006 090000-BBBB.m4a', '', 812100000, 3.2, 'BBBB')")
    c.commit()
    c.close()


def test_voicememo_import(make_client, tmp_path):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    root = tmp_path / "vm"
    app.state.voicememos_root = root
    with client:
        assert client.get("/api/voicememos").json()["status"] == "missing"
        _fake_voicememos(root)
        items = client.get("/api/voicememos").json()["items"]
        assert [m["title"] for m in items] == ["음성 메모", "소비자행동 5주차"]  # 새것부터, 제목 없으면 '음성 메모'
        assert items[1]["date"] == 812000000 + 978307200 and not items[1]["imported"]

        r = client.post("/api/voicememos/import", json={"keys": ["AAAA"], "subject": "소비자행동"})
        assert r.status_code == 201 and len(r.json()) == 1
        app.state.worker.wait_idle()
        rec = client.get(f"/api/recordings/{r.json()[0]['id']}").json()
        assert rec["status"] == "done" and rec["title"] == "소비자행동 5주차" and rec["source"] == "voicememo"
        assert rec["created_at"] == 812000000 + 978307200 and rec["subject"] == "소비자행동"
        # 두 번 가져오지 않는다
        assert client.post("/api/voicememos/import", json={"keys": ["AAAA"]}).json() == []
        assert [m["imported"] for m in client.get("/api/voicememos").json()["items"]] == [False, True]


def test_voicememo_no_permission(make_client, monkeypatch):
    from app import voicememos
    client, _ = make_client(FakePipeline())

    def denied(*a, **k):
        raise voicememos.NoPermission()
    monkeypatch.setattr(voicememos, "list_memos", denied)
    with client:
        assert client.get("/api/voicememos").json()["status"] == "permission"
        assert client.post("/api/voicememos/import", json={"keys": ["x"]}).status_code == 403


def test_reminders(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        items = client.get("/api/reminders").json()
        preset = next(r for r in items if r["id"] == "2026-ku-convergence")
        assert preset["date"] == "2026-10-14" and preset["hours"] == [9, 12, 15, 18, 21]
        assert not preset["done"] and not preset["muted"] and preset["link"].startswith("https://")

        r = client.post("/api/reminders", json={"title": " 과제 마감 ", "date": "2026-11-01", "link": "blackboard.korea.ac.kr"})
        assert r.status_code == 201 and r.json()["title"] == "과제 마감" and r.json()["link"] == "https://blackboard.korea.ac.kr"
        assert client.post("/api/reminders", json={"title": "x", "date": "내일"}).status_code == 400
        assert client.post("/api/reminders", json={"title": " ", "date": "2026-11-01"}).status_code == 400

        assert client.patch("/api/reminders/2026-ku-convergence", json={"muted": True}).json()["muted"] is True
        done = client.patch("/api/reminders/2026-ku-convergence", json={"done": True}).json()
        assert done["done"] and done["done_at"]
        assert client.patch("/api/reminders/2026-ku-convergence", json={"done": False}).json()["done"] is False

        assert client.delete("/api/reminders/2026-ku-convergence").status_code == 204
        assert all(r["id"] != "2026-ku-convergence" for r in client.get("/api/reminders").json())
        assert client.patch("/api/reminders/2026-ku-convergence", json={"done": True}).status_code == 404


def test_reminder_preset_not_recreated_after_delete(make_client):
    client, _ = make_client(FakePipeline())
    with client:
        client.delete("/api/reminders/2026-ku-convergence")
    client2, _ = make_client(FakePipeline())   # 앱을 다시 켜도
    with client2:
        assert all(r["id"] != "2026-ku-convergence" for r in client2.get("/api/reminders").json())


class SlowPipeline(FakePipeline):
    """처리 순서를 기록하고, 첫 작업은 신호를 받을 때까지 붙잡아 둔다."""
    def __init__(self):
        super().__init__()
        import threading
        self.gate = threading.Event()
        self.order = []

    def process(self, audio, **kw):
        if not self.order:
            self.gate.wait(10)
        self.order.append(len(audio))
        return super().process(audio, **kw)


def test_queue_order_prioritize_and_eta(make_client, tmp_path):
    import shutil, subprocess, imageio_ffmpeg
    # 길이가 다른 파일 3개 → 처리 순서를 길이로 알아본다
    files = []
    for sec in (1, 2, 3):
        out = tmp_path / f"t{sec}.wav"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={sec}", "-ar", "16000", str(out)], check=True)
        files.append(out)
    pipe = SlowPipeline()
    client, app = make_client(pipe)
    with client:
        ids = []
        for f in files:
            with open(f, "rb") as fh:
                ids.append(client.post("/api/recordings", files={"file": (f.name, fh, "audio/wav")}).json()["id"])
        time.sleep(0.3)  # 첫 번째가 처리 시작
        recs = {r["id"]: r for r in client.get("/api/recordings").json()}
        assert recs[ids[0]]["status"] == "processing" and "eta_end" in recs[ids[0]]
        assert recs[ids[1]]["queue_pos"] == 1 and recs[ids[2]]["queue_pos"] == 2
        assert recs[ids[1]]["eta_start"] < recs[ids[2]]["eta_start"]
        assert 1.9 < recs[ids[2]]["duration"] < 3.1   # 올릴 때 길이를 미리 읽어 둠

        # 세 번째를 먼저
        assert client.post(f"/api/recordings/{ids[2]}/prioritize").json()["order"] == [ids[2], ids[1]]
        assert client.post(f"/api/recordings/{ids[0]}/prioritize").status_code == 409
        pipe.gate.set()
        app.state.worker.wait_idle()
        n = 16000
        assert pipe.order == [1 * n, 3 * n, 2 * n]


def test_voicememo_auto_import(make_client, tmp_path):
    import sqlite3
    pipe = FakePipeline()
    client, app = make_client(pipe)
    root = tmp_path / "vm"
    app.state.voicememos_root = root
    _fake_voicememos(root)   # 예전 음성 메모 2개 (2026년 9월쯤)
    with client:
        st = client.get("/api/voicememos/auto").json()
        assert st["enabled"] is False
        assert client.post("/api/voicememos/auto/scan").json()["imported"] == 0   # 꺼져 있으면 안 가져옴

        on = client.post("/api/voicememos/auto", json={"enabled": True}).json()
        assert on["enabled"] and on["since"] > 0
        # 켜기 전에 녹음한 것은 가져오지 않는다
        client.post("/api/voicememos/auto/scan")
        assert client.post("/api/voicememos/auto/scan").json()["imported"] == 0

        # 켠 뒤 아이폰에서 새로 녹음 → 맥으로 내려옴
        rec_dir = root / "Recordings"
        shutil.copy(WAV, rec_dir / "20261011 093000-CCCC.m4a")
        c = sqlite3.connect(rec_dir / "CloudRecordings.db")
        c.execute("INSERT INTO ZCLOUDRECORDING VALUES (3, '20261011 093000-CCCC.m4a', '마케팅원론', ?, 3.2, 'CCCC')",
                  (time.time() - 978307200,))
        c.commit()
        c.close()
        assert client.post("/api/voicememos/auto/scan").json()["imported"] == 0   # 처음 본 파일은 한 번 더 확인
        r = client.post("/api/voicememos/auto/scan").json()
        assert r["imported"] == 1 and r["status"] == "ok"
        app.state.worker.wait_idle()
        recs = client.get("/api/recordings").json()
        assert [x["title"] for x in recs] == ["마케팅원론"] and recs[0]["status"] == "done"

        # 까치녹음기에서 지워도 다시 들어오지 않는다
        client.delete(f"/api/recordings/{recs[0]['id']}")
        client.post("/api/voicememos/auto/scan")
        assert client.post("/api/voicememos/auto/scan").json()["imported"] == 0
        assert client.get("/api/recordings").json() == []

        off = client.post("/api/voicememos/auto", json={"enabled": False}).json()
        assert off["enabled"] is False


def test_voicememo_auto_no_permission(make_client, monkeypatch):
    from app import voicememos
    client, _ = make_client(FakePipeline())

    def denied(*a, **k):
        raise voicememos.NoPermission()
    monkeypatch.setattr(voicememos, "list_memos", denied)
    with client:
        client.post("/api/voicememos/auto", json={"enabled": True})
        r = client.post("/api/voicememos/auto/scan").json()
        assert r["imported"] == 0 and r["status"] == "permission"


def test_reminder_range_and_preset_update(make_client, tmp_path):
    import sqlite3
    client, _ = make_client(FakePipeline())
    with client:
        preset = next(r for r in client.get("/api/reminders").json() if r["id"] == "2026-ku-convergence")
        assert preset["date"] == "2026-10-14" and preset["end_date"] == "2026-10-16" and preset["popup"] is True
        r = client.post("/api/reminders", json={"title": "시험", "date": "2026-11-01", "end_date": "2026-11-03", "popup": True}).json()
        assert r["end_date"] == "2026-11-03" and r["popup"]
        assert client.post("/api/reminders", json={"title": "x", "date": "2026-11-03", "end_date": "2026-11-01"}).status_code == 400

    # 1.0.2 에서 미리 넣어 둔 알림(기간·안내 창 없음)이 있던 맥: 새 버전이 켜지면 기간·안내 창이 붙는다
    db = sqlite3.connect(tmp_path / "kkachi.db")
    db.execute("UPDATE reminders SET end_date = NULL, popup = 0, muted = 1 WHERE id = '2026-ku-convergence'")
    db.commit()
    db.close()
    client2, _ = make_client(FakePipeline())
    with client2:
        preset = next(r for r in client2.get("/api/reminders").json() if r["id"] == "2026-ku-convergence")
        assert preset["end_date"] == "2026-10-16" and preset["popup"] and preset["muted"]   # 끈 건 그대로
