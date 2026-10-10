import os
import shutil
import time

from app.inbox import Inbox
from tests.test_api import WAV, FakePipeline, make_client  # noqa: F401 (pytest fixture)
from app.main import create_app
from fastapi.testclient import TestClient


def test_inbox_waits_until_file_settles_and_moves_it(tmp_path):
    icloud = tmp_path / "CloudDocs"
    icloud.mkdir()
    got = []
    box = Inbox(icloud / "까치녹음기", lambda p: got.append(p.name))
    assert box.scan() == 0 and box.status == "ok" and box.root.is_dir()   # 폴더를 만들어 줌

    f = box.root / "강의.m4a"
    shutil.copy(WAV, f)
    (box.root / "메모.txt").write_text("x")              # 소리 파일이 아니면 무시
    (box.root / ".다른.m4a.icloud").write_bytes(b"x")    # 아직 안 내려온 파일은 무시
    now = time.time()
    assert box.scan(now) == 0                    # 처음 본 파일은 한 번 더 확인
    assert box.scan(now + 0.5) == 0              # 방금 저장된 파일은 기다림
    os.utime(f, (now - 30, now - 30))
    box.scan(now)
    assert box.scan(now) == 1 and got == ["강의.m4a"]
    assert not f.exists() and (box.root / "가져온 파일" / "강의.m4a").exists()
    assert box.scan(now) == 0


def test_inbox_no_icloud(tmp_path):
    box = Inbox(tmp_path / "없음" / "까치녹음기", lambda p: None)
    assert box.scan() == 0 and box.status == "no-icloud"


def test_inbox_imports_into_queue(tmp_path):
    pipe = FakePipeline()
    inbox_dir = tmp_path / "CloudDocs" / "까치녹음기"
    inbox_dir.mkdir(parents=True)
    app = create_app(data_dir=tmp_path / "data", pipeline_factory=lambda: pipe, inbox_dir=inbox_dir)
    app.state.inbox.interval = 3600   # 자동 확인 대신 직접 부른다
    with TestClient(app) as client:
        f = inbox_dir / "IMG_1234.m4a"
        shutil.copy(WAV, f)
        old = time.time() - 60
        os.utime(f, (old, old))
        client.post("/api/inbox/scan")
        assert client.post("/api/inbox/scan").json()["imported"] == 1
        app.state.worker.wait_idle()
        rec = client.get("/api/recordings").json()[0]
        assert rec["title"] == "IMG_1234" and rec["source"] == "iphone" and rec["status"] == "done"
        assert abs(rec["created_at"] - old) < 2
        st = client.get("/api/inbox").json()
        assert st["status"] == "ok" and st["imported"] == 1
        # 같은 파일을 또 보내도(옮기기 실패 등) 두 번 가져오지 않는다
        shutil.copy(inbox_dir / "가져온 파일" / "IMG_1234.m4a", f)
        os.utime(f, (old, old))
        client.post("/api/inbox/scan")
        client.post("/api/inbox/scan")
        assert len(client.get("/api/recordings").json()) == 1
