"""오디오 파일을 16kHz 모노 float32 배열로 디코딩한다 (번들된 ffmpeg 사용)."""

import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

from app.config import SAMPLE_RATE


class AudioDecodeError(RuntimeError):
    pass


def decode(path: str | Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-loglevel", "error",
        "-i", str(path),
        "-vn",  # 영상 파일이면 소리만
        "-ac", "1",
        "-ar", str(sr),
        "-f", "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioDecodeError(proc.stderr.decode(errors="replace").strip())
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise AudioDecodeError("소리가 없는 파일입니다.")
    return audio


def duration(audio: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    return len(audio) / sr


def probe_duration(path: str | Path) -> float | None:
    """디코딩하지 않고 파일 머리말에서 길이(초)만 읽는다. 대기열 남은 시간 계산용. 모르면 None."""
    import re

    try:
        proc = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path)],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)
