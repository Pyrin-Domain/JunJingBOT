"""下载系统：所有“把网络资源变成文件”的逻辑都在这里。

对外：

    from download_system import DownloadService, DownloadTask, DownloadStorage

    service = DownloadService(DownloadStorage(tmp_dir, output_dir))
    result = await service.download_bilibili(url)
    path = result.path
"""

from .mux import MediaToolError, ffmpeg_path, ffprobe_path, has_audio_stream, merge_video_audio
from .pool import DownloadCancelled, DownloadError, DownloadPool, DownloadProvider
from .service import DownloadService
from .storage import DownloadStorage
from .task import DownloadResult, DownloadStatus, DownloadTask

__all__ = [
    "DownloadService",
    "DownloadPool",
    "DownloadProvider",
    "DownloadTask",
    "DownloadResult",
    "DownloadStatus",
    "DownloadStorage",
    "DownloadError",
    "DownloadCancelled",
    "MediaToolError",
    "ffmpeg_path",
    "ffprobe_path",
    "has_audio_stream",
    "merge_video_audio",
]
