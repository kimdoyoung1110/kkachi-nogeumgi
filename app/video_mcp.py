"""까치 영상분석기를 Claude 앱에서 부를 수 있게 하는 MCP 서버.

Claude 채팅에 영상 주소를 주면 이 맥에서 컷·화면·글자·내레이션을 뽑아 Claude 에게 돌려준다.
분석은 별도 작업(video_job)이 하고, 도구는 길어야 WAIT_SECONDS 만 기다린 뒤 진행 상황을 알려 준다.
    uv run python -m app.video_mcp
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image

from app import system, video

MAX_SHEETS = 4   # 한 번에 돌려줄 이미지 수. 나머지는 get_contact_sheet 로

mcp = FastMCP("kkachi-video", instructions=(
    "광고 영상을 컷 단위로 분석하는 도구입니다. 영상 주소나 맥의 영상 파일 경로를 받으면 analyze_video 를 먼저 부르세요. "
    "결과의 이미지는 한 줄이 한 컷이고, 왼쪽에 컷 번호·시작 시각·길이, 오른쪽에 그 컷의 시작·중간·끝 화면이 있습니다. "
    "내레이션은 음악에 섞이면 틀릴 수 있으니 화면 글자와 비교해서 읽으세요. "
    "컷 안에서 움직임이나 전환을 자세히 볼 때는 look_closer 를 쓰세요. "
    "컷 사진이 들어간 PDF 는 export_pdf 로 만들고, 기존 PDF 에 붙일 때는 append_to 를 쓰세요. "
    "주소 없이 예전 영상을 말하면 list_videos 로 찾으세요. "
    "긴 영상은 analyze_video 가 '아직 분석 중'이라고 답하면 끝날 때까지 check_video 를 반복해서 부르세요."
))

WAIT_SECONDS = float(os.environ.get("KKACHI_VIDEO_WAIT", 40))   # 도구 한 번에 기다리는 시간 (Claude 앱이 끊기 전에 답한다)


def _jpeg(path) -> Image:
    return Image(path=str(path))


def _start(source: str, split: str) -> str:
    """분석을 별도 작업으로 띄운다. 이미 끝났거나 누가 분석 중이면 새로 띄우지 않는다."""
    if split not in video.SPLITS:
        raise ValueError(f"나누는 방식은 {', '.join(video.SPLITS)} 중 하나예요.")
    vid = video.video_id(source)
    folder = video.VIDEOS_DIR / vid
    meta = video.read_json(folder / "shots.json")
    if video.active(folder) or (meta and meta.get("split", "cut") == split):
        return vid
    job = subprocess.Popen([sys.executable, "-m", "app.video_job", source, split], cwd=str(system.REPO_DIR),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)   # Claude 앱이 도구를 끊거나 꺼져도 계속 돈다
    video.set_status(folder, "queued", source=source, split=split, pid=job.pid)
    # 끝난 작업을 거둬들인다. 안 그러면 꺼진 작업이 남아(좀비) 계속 분석 중인 것처럼 보인다
    threading.Thread(target=job.wait, name=f"reap-{vid}", daemon=True).start()
    return vid


async def _wait(vid: str, ctx: Context) -> list:
    """끝날 때까지 최대 WAIT_SECONDS 기다린다. 아직이면 진행 상황을 알려 주고 check_video 로 이어서 기다리게 한다."""
    folder = video.VIDEOS_DIR / vid
    end = anyio.current_time() + WAIT_SECONDS
    while True:
        status = video.active(folder)
        if status is None:
            break
        await ctx.report_progress(round(status.get("progress", 0) * 100), 100, status.get("step") or "기다리는 중")
        if anyio.current_time() >= end:
            pct = round(status.get("progress", 0) * 100)
            return [f"아직 분석 중이에요: {status.get('step') or '기다리는 중'} ({pct}%). 영상 번호 {vid}. "
                    f"check_video(video_id='{vid}') 를 불러서 이어서 기다려 주세요. 긴 영상은 몇 분 걸려요. "
                    "분석은 이 맥에서 따로 계속되니 analyze_video 를 다시 부르지 않아도 돼요."]
        await anyio.sleep(1)
    meta = video.read_json(folder / "shots.json")
    if meta is None:
        failed = video.read_json(folder / "status.json") or {}
        raise ValueError(failed.get("error") or "영상을 분석하지 못했어요. 다시 시도해 주세요.")
    pages = video.sheets(vid)
    note = "" if len(pages) <= MAX_SHEETS else f"\n\n이미지 {len(pages)}장 중 {MAX_SHEETS}장만 보냈어요. 나머지는 get_contact_sheet 로 보세요."
    return [video.shot_table(meta) + note, *[_jpeg(p) for p in pages[:MAX_SHEETS]]]


@mcp.tool()
async def analyze_video(source: str, ctx: Context, split: str = "auto") -> list:
    """영상 주소(유튜브·인스타 릴스 등) 또는 맥의 영상 파일 경로를 받아 구간별 샷 리스트와 이미지를 돌려준다.
    split: 'auto' 컷으로 나누고 3초 넘는 장면은 1초씩 더 나눔(기본) / 'cut' 컷마다만 /
    '0.5'·'1'·'2' 그 초마다 일정하게. 사용자가 '초 단위로', '1초마다' 등을 말하면 맞춰 주세요.
    결과 표의 '번호'는 구간 번호(look_closer·export_pdf 에서 쓰는 번호), '컷'은 원래 컷 번호예요 (3-2 = 3번 컷의 둘째 구간).
    긴 영상은 한 번에 끝나지 않으면 진행 상황을 돌려줘요. 그때는 check_video 로 이어서 기다리세요."""
    return await _wait(await anyio.to_thread.run_sync(_start, source, split), ctx)


@mcp.tool()
async def check_video(video_id: str, ctx: Context) -> list:
    """analyze_video 가 '아직 분석 중'이라고 했을 때 불러서 이어서 기다린다. 끝나면 샷 리스트와 이미지를 돌려준다."""
    if not video.valid_id(video_id):
        raise ValueError(f"영상 번호가 올바르지 않아요: {video_id}")
    return await _wait(video_id, ctx)


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
