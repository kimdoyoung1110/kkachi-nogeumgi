from fastapi.testclient import TestClient

from tests.test_api import WAV, FakePipeline, make_client  # noqa: F401 (pytest fixture)


def test_phone_upload(make_client):
    pipe = FakePipeline()
    client, app = make_client(pipe)
    phone = TestClient(app.state.phone_app)
    with client:
        # 꺼져 있으면 받지 않는다
        assert phone.post("/upload?pin=000000", content=b"x").status_code == 403
        st = client.post("/api/phone", json={"enabled": True}).json()
        pin = st["pin"]
        assert st["enabled"] and len(pin) == 6 and all(u.endswith(f"/upload?pin={pin}") for u in st["urls"])

        assert phone.get(f"/?pin={pin}").status_code == 200
        assert phone.post("/upload?pin=999999x", content=b"x").status_code == 403

        # 단축어 '양식' 본문 (file 필드)
        with open(WAV, "rb") as f:
            r = phone.post(f"/upload?pin={pin}", files={"file": ("새로운 녹음 2.m4a", f, "audio/mp4")})
        assert r.status_code == 200 and "새로운 녹음 2" in r.text
        # 단축어 '파일' 본문 (그대로)
        with open(WAV, "rb") as f:
            r = phone.post(f"/upload?pin={pin}&name=마케팅.wav", content=f.read())
        assert r.status_code == 200
        app.state.worker.wait_idle()
        recs = client.get("/api/recordings").json()
        assert sorted(x["title"] for x in recs) == ["마케팅", "새로운 녹음 2"]
        assert all(x["status"] == "done" and x["source"] == "phone" for x in recs)

        # 녹음 없이 실행해서 빈 양식만 온 경우, 소리가 아닌 파일 → 목록에 넣지 않고 거절
        r = phone.post(f"/upload?pin={pin}", data={"file": ""})
        assert r.status_code == 400 and "녹음 파일이 오지 않았어요" in r.text
        r = phone.post(f"/upload?pin={pin}", files={"file": ("메모.txt", b"hello" * 400, "text/plain")})
        assert r.status_code == 400 and "소리 파일이 아니에요" in r.text
        assert len(client.get("/api/recordings").json()) == 2

        # 본체 API 는 창구로 못 쓴다
        assert phone.get(f"/api/recordings?pin={pin}").status_code == 404

        # PIN 을 바꾸면 예전 주소는 안 됨
        new = client.post("/api/phone/pin").json()["pin"]
        assert phone.get(f"/?pin={pin}").status_code == 403 or new == pin
        client.post("/api/phone", json={"enabled": False})
        assert phone.get(f"/?pin={new}").status_code == 403


def test_phone_locks_after_many_wrong_pins(make_client):
    client, app = make_client(FakePipeline())
    phone = TestClient(app.state.phone_app)
    with client:
        pin = client.post("/api/phone", json={"enabled": True}).json()["pin"]
        for _ in range(10):
            phone.get("/?pin=wrong")
        assert phone.get(f"/?pin={pin}").status_code == 429   # 맞는 PIN 이어도 잠시 막힘
