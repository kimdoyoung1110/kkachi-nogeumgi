"""까치 영상분석기를 Claude 앱에서 부를 수 있게 하는 MCP 서버 (시험판).

Claude 채팅에 영상 주소를 주면 이 맥에서 컷·화면·글자·내레이션을 뽑아 Claude 에게 돌려준다.
    uv run --group video python -m app.video_mcp
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
import threading
from typing import Any, Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image

from app import video

MAX_SHEETS = 4   # 한 번에 돌려줄 이미지 수. 나머지는 get_contact_sheet 로

mcp = FastMCP("kkachi-video", instructions=(
    "광고 영상을 컷 단위로 분석하는 도구입니다. 영상 주소나 맥의 영상 파일 경로를 받으면 analyze_video 를 먼저 부르세요. "
    "결과의 이미지는 한 줄이 한 컷이고, 왼쪽에 컷 번호·시작 시각·길이, 오른쪽에 그 컷의 시작·중간·끝 화면이 있습니다. "
    "내레이션은 음악에 섞이면 틀릴 수 있으니 화면 글자와 비교해서 읽으세요. "
    "컷 안에서 움직임이나 전환을 자세히 볼 때는 look_closer 를 쓰세요. "
    "컷 사진이 들어간 PDF 는 export_pdf 로 만들고, 기존 PDF 에 붙일 때는 append_to 를 쓰세요. "
    "주소 없이 예전 영상을 말하면 list_videos 로 찾으세요."
))

_pipeline: Optional[Any] = None
_lock = threading.Lock()


def _run(source: str, split: str, tell) -> dict[str, Any]:
    global _pipeline
    # 음성 모델이 표준 출력에 글을 쓰면 MCP 통신이 깨지므로 오류 출력으로 돌린다
    with _lock, contextlib.redirect_stdout(sys.stderr):
        if _pipeline is None:
            from app.pipeline import Pipeline
            _pipeline = Pipeline()
        return video.analyze(source, pipeline=_pipeline, progress=tell, split=split)


def _jpeg(path) -> Image:
    return Image(path=str(path))


@mcp.tool()
async def analyze_video(source: str, ctx: Context, split: str = "auto") -> list:
    """영상 주소(유튜브·인스타 릴스 등) 또는 맥의 영상 파일 경로를 받아 구간별 샷 리스트와 이미지를 돌려준다.
    split: 'auto' 컷으로 나누고 3초 넘는 장면은 1초씩 더 나눔(기본) / 'cut' 컷마다만 /
    '0.5'·'1'·'2' 그 초마다 일정하게. 사용자가 '초 단위로', '1초마다' 등을 말하면 맞춰 주세요.
    결과 표의 '번호'는 구간 번호(look_closer·export_pdf 에서 쓰는 번호), '컷'은 원래 컷 번호예요 (3-2 = 3번 컷의 둘째 구간)."""
    steps = iter(range(1, 10))

    def tell(text: str) -> None:
        anyio.from_thread.run(ctx.report_progress, next(steps), 6, text)

    meta = await anyio.to_thread.run_sync(_run, source, split, tell)
    pages = video.sheets(meta["id"])
    note = "" if len(pages) <= MAX_SHEETS else f"\n\n이미지 {len(pages)}장 중 {MAX_SHEETS}장만 보냈어요. 나머지는 get_contact_sheet 로 보세요."
    return [video.shot_table(meta) + note, *[_jpeg(p) for p in pages[:MAX_SHEETS]]]


@mcp.tool()
def get_contact_sheet(video_id: str, page: int) -> list:
    """구간 이미지의 page 번째 장을 돌려준다 (1부터). 한 장에 가로 영상은 6구간, 세로 영상은 4구간."""
    pages = video.sheets(video_id)
    if not 1 <= page <= len(pages):
        raise ValueError(f"이미지는 1~{len(pages)}장까지 있어요.")
    return [f"{page}/{len(pages)}장", _jpeg(pages[page - 1])]


@mcp.tool()
def look_closer(video_id: str, start: float, end: float, frames: int = 6) -> list:
    """start~end 초 구간의 화면을 고르게 frames 장(최대 12) 꺼낸다. 카메라 무빙, 전환, 놓친 컷을 볼 때 쓴다."""
    out: list = []
    for t, path in video.frames_between(video_id, start, end, frames):
        out += [f"{t:.2f}초", _jpeg(path)]
    return out


@mcp.tool()
def list_videos() -> str:
    """지금까지 분석한 영상 목록(영상 번호, 제목, 길이, 컷 수). 예전 영상을 찾을 때 먼저 부른다."""
    rows = [f"- {v['id']}: {v['title']} ({v['duration']:.0f}초, 컷 {v['shots']}개)"
            for v in video.list_videos() if v["state"] == "done"]
    return "\n".join(rows) or "아직 분석한 영상이 없어요."


@mcp.tool()
def export_pdf(video_id: str, shots: Optional[list[int]] = None, append_to: Optional[str] = None) -> str:
    """컷별 스냅샷, 화면 글자, 내레이션을 담은 샷 리스트 PDF 를 맥의 다운로드 폴더에 만든다.
    컷 사진이 들어간 PDF 를 원하면 직접 만들지 말고 이 도구를 쓰세요. 스냅샷 파일은 이 맥에만 있습니다.
    shots: 넣을 구간 번호들 (분석 표의 '번호'). 없으면 전부.
    append_to: 기존 PDF 의 전체 경로나 이름 일부. 주면 그 PDF 뒤에 컷을 붙인 새 파일을 만든다 (원본은 그대로).
    이름은 다운로드·문서·데스크탑 폴더에서 찾는다."""
    meta = video.pick_shots(video.load(video_id), shots)
    folder = video.VIDEOS_DIR / video_id
    downloads = Path.home() / "Downloads"
    if not append_to:
        out = video.free_path(downloads / video.pdf_name(meta))
        video.make_pdf(meta, folder, out)
        return f"다운로드 폴더에 저장했어요: {out}"
    home = Path.home()
    original = video.find_pdf(append_to, [downloads, home / "Documents", home / "Desktop"])
    added = video.make_pdf(meta, folder, folder / "added.pdf")
    out = video.free_path(original.with_name(f"{original.stem} (컷 추가).pdf"))
    video.join_pdfs(original, added, out)
    added.unlink(missing_ok=True)
    return f"{original.name} 뒤에 컷 {len(meta['shots'])}개를 붙여 새 파일로 저장했어요. 원본은 그대로예요: {out}"


if __name__ == "__main__":
    mcp.run()
