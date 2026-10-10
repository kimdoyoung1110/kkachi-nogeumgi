"""아이폰에서 와이파이로 바로 보내기 (iCloud 없이).

까치녹음기 본체(127.0.0.1:8765)는 맥 안에서만 열고, 아이폰이 파일을 올릴 '창구'만 따로 연다.
- '와이파이로 받기'를 켰을 때만 같은 와이파이(0.0.0.0:8770)에 열린다
- 할 수 있는 건 파일 올리기 하나뿐 (녹음 목록·받아쓴 글은 못 봄)
- 주소에 6자리 PIN 이 맞아야 받고, 여러 번 틀리면 그 기기를 잠시 막는다

아이폰 단축어: 음성 메모 공유 › '까치녹음기로 보내기' → URL 콘텐츠 가져오기(POST, 양식 file=단축어 입력)
    http://<맥 이름>.local:8770/upload?pin=123456
"""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

log = logging.getLogger(__name__)

PORT = 8770
MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2GB
MAX_FAILS = 10                        # PIN 을 이만큼 틀리면
LOCK_SECONDS = 600                    # 이만큼 막는다


def new_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def addresses(port: int = PORT) -> list[str]:
    """아이폰 단축어에 넣을 맥 주소. '<맥 이름>.local' 이 IP 가 바뀌어도 그대로라 먼저."""
    out = []
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=3).stdout.strip()
        if name:
            out.append(f"http://{name}.local:{port}")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        # 실제로 보내지는 않고, 바깥으로 나갈 때 쓰는 내 주소만 알아낸다
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        if not ip.startswith("127."):
            out.append(f"http://{ip}:{port}")
    except OSError:
        pass
    return out


def make_app(get_pin: Callable[[], Optional[str]], save: Callable[[str, Path], dict], tmp_dir: Path,
             probe: Optional[Callable[[Path], Optional[float]]] = None) -> FastAPI:
    """save(파일 이름, 받은 파일 경로) → 만든 녹음. get_pin() 이 None 이면 꺼진 것.
    probe(파일) 이 None 이면 소리 파일이 아니라고 보고 받지 않는다."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def plain_error(request: Request, exc: HTTPException) -> PlainTextResponse:
        # 아이폰 단축어가 응답을 알림으로 그대로 보여주므로 JSON 대신 글자만
        return PlainTextResponse(f"⚠️ {exc.detail}", status_code=exc.status_code)
    fails: dict[str, tuple[int, float]] = {}
    lock = threading.Lock()

    def check(request: Request) -> None:
        pin = get_pin()
        if pin is None:
            raise HTTPException(403, "맥의 까치녹음기에서 '와이파이로 받기'가 꺼져 있어요.")
        who = request.client.host if request.client else "?"
        with lock:
            n, since = fails.get(who, (0, 0.0))
            if n >= MAX_FAILS and time.time() - since < LOCK_SECONDS:
                raise HTTPException(429, "PIN 을 여러 번 틀려서 잠시 막았어요. 10분 뒤에 다시 해주세요.")
            if not secrets.compare_digest(request.query_params.get("pin", ""), pin):
                fails[who] = (n + 1 if time.time() - since < LOCK_SECONDS else 1, time.time())
                raise HTTPException(403, "PIN 이 맞지 않아요. 맥의 까치녹음기에 보이는 주소를 그대로 넣어주세요.")
            fails.pop(who, None)

    @app.get("/", response_class=PlainTextResponse)
    def hello(request: Request) -> str:
        check(request)
        return "까치녹음기와 연결됐어요 🐦‍⬛"

    @app.post("/upload", response_class=PlainTextResponse)
    async def upload(request: Request) -> str:
        check(request)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"phone-{secrets.token_hex(6)}.part"
        name = request.query_params.get("name") or ""
        size = 0
        try:
            ctype = request.headers.get("content-type", "")
            if ctype.startswith("multipart/form-data"):
                # 단축어 '양식' 본문: 첫 번째 파일을 받는다
                form = await request.form(max_part_size=MAX_BYTES)
                part = next((v for v in form.values() if hasattr(v, "read")), None)
                if part is None:
                    raise HTTPException(400, "보낸 파일이 없어요. 단축어의 '양식'에 파일을 넣어주세요.")
                name = name or part.filename or ""
                with open(tmp, "wb") as out:
                    while chunk := await part.read(1024 * 1024):
                        size += len(chunk)
                        out.write(chunk)
            else:
                # 단축어 '파일' 본문: 그대로 받는다
                with open(tmp, "wb") as out:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise HTTPException(413, "파일이 너무 커요.")
                        out.write(chunk)
            if size < 1024:
                # 공유로 받은 녹음 없이 실행했거나, 단축어 양식의 file 항목이 '파일'이 아니라 글자로 들어간 경우
                raise HTTPException(400, "녹음 파일이 오지 않았어요. 음성 메모에서 공유 › 까치녹음기로 보내기로 보내주세요. "
                                         "(단축어 양식의 file 항목은 '파일' 종류여야 해요)")
            if probe and probe(tmp) is None:
                raise HTTPException(400, "소리 파일이 아니에요. 음성 메모의 녹음을 보내주세요.")
            rec = save(name or "아이폰 녹음.m4a", tmp)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("아이폰에서 와이파이로 받음: %s (%s바이트)", rec["title"], size)
        return f"까치녹음기로 보냈어요: {rec['title']}\n받아쓰기가 끝나면 맥에서 알려줄게요 🐦‍⬛"

    return app


class Listener:
    """창구 서버를 켜고 끈다 (별도 스레드의 uvicorn)."""

    def __init__(self, app: FastAPI, port: int = PORT):
        self.app = app
        self.port = port
        self._server = None
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        import uvicorn

        if self._server is not None:
            return
        # 포트가 이미 쓰이고 있으면 알려준다
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", self.port))
            except OSError:
                self.error = f"{self.port}번 통로를 다른 프로그램이 쓰고 있어요."
                log.warning("와이파이로 받기를 켜지 못함: %s", self.error)
                return
        self.error = None
        server = uvicorn.Server(uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning",
                                               access_log=False, lifespan="off"))
        threading.Thread(target=server.run, name="kkachi-phone", daemon=True).start()
        self._server = server
        log.info("와이파이로 받기 켬 (포트 %s)", self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server = None
            log.info("와이파이로 받기 끔")
