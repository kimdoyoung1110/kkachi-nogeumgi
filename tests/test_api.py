import threading

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
        assert rec["title"] == "강의 1" and rec["status"] == "queued"
        assert app.state.worker.wait_idle()

        d = client.get(f"/api/recordings/{rec['id']}").json()
        assert d["status"] == "done" and d["progress"] == 1.0 and d["stage_label"] == "완료"
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
