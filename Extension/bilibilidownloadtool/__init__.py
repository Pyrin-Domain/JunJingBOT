"""``bilibilidownloadtool``：接入本项目的 B 站解析 / 下载引擎。

上游仓库：https://github.com/NANblogink/bilibilidownloadtool （MIT）
这里只保留无界面运行所需的解析器与配置层，GUI 文件已裁掉，详见 ``VENDOR.md``。

对外只暴露 :mod:`api` 里的对象；本文件的 import 很轻，不会加载上游解析器。
"""

from .api import (
    BiliClient,
    BiliDownloadError,
    BiliError,
    BiliParseError,
    BiliUnsupported,
    DownloadOutcome,
    Quality,
    VideoInfo,
)

__all__ = [
    "BiliClient",
    "BiliError",
    "BiliParseError",
    "BiliDownloadError",
    "BiliUnsupported",
    "VideoInfo",
    "Quality",
    "DownloadOutcome",
]
