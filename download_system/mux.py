"""ffmpeg / ffprobe 小封装。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class MediaToolError(RuntimeError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def has_audio_stream(file_path: str | Path) -> bool:
    probe = ffprobe_path()
    if not probe:
        return False
    try:
        result = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def merge_video_audio(
    video: str | Path,
    audio: str | Path | None,
    output: str | Path,
    *,
    timeout: float = 300.0,
) -> Path:
    """用 ffmpeg 合并；失败抛 MediaToolError，由调用方决定是否回退。"""
    tool = ffmpeg_path()
    if not tool:
        raise MediaToolError("找不到 ffmpeg")
    args = [tool, "-y", "-i", str(video)]
    if audio is not None:
        # 显式取第一个视频流 + 外部音频的第一个音轨，避免 ffmpeg 自己
        # 从视频文件里挑到一条不带声音的轨道。
        args += [
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ]
    else:
        args += ["-c", "copy"]
    args += [str(output)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaToolError(str(exc)) from exc
    if result.returncode != 0:
        raise MediaToolError(result.stderr[-800:] or "ffmpeg 合并失败")
    return Path(output)


__all__ = [
    "MediaToolError",
    "ffmpeg_path",
    "ffprobe_path",
    "has_audio_stream",
    "merge_video_audio",
]
