"""영상 하나를 분석하는 별도 작업 (Claude 앱의 도구가 띄운다).

Claude 앱은 도구 하나를 오래 기다려 주지 않아서, 긴 영상은 이 작업이 따로 끝까지 분석하고
도구는 videos/<id>/status.json 으로 진행 상황만 확인한다. Claude 앱을 꺼도 이 작업은 계속된다.
    python -m app.video_job <주소 또는 파일> <나누는 방식>
"""

from __future__ import annotations

import logging
import sys

from app import config, video


def main(source: str, split: str) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=config.LOGS_DIR / "video-job.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app.pipeline import Pipeline

    try:
        video.analyze(source, pipeline=Pipeline(), split=split)
    except Exception:
        logging.getLogger(__name__).exception("영상 분석 실패: %s", source)   # 실패 내용은 status.json 에도 남는다


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
