"""第三方开源仓库的接入层。

目录约定：``Extension/<仓库名>/`` 原样存放上游代码，只额外增加
``__init__.py`` 与 ``api.py`` 作为无界面桥接；调上游内部实现的地方都写在
各自仓库的 ``VENDOR.md`` 里，便于以后按 commit 升级。

已接入：

- ``jm_crawer``            — JM 漫画下载（占位源）
- ``bilibilidownloadtool`` — B 站解析 / 下载 / 合并（PyQt5 程序的无界面裁剪版）

``bilibilidownloadtool`` 走惰性暴露：它带 50 万字符的解析器，启动时不该为它
付出导入代价。
"""

from .jm_crawer import get_jmcomic_pdf

__all__ = ["get_jmcomic_pdf", "bilibili"]

_BILIBILI_EXPORTS = {
    "BiliClient",
    "BiliError",
    "BiliParseError",
    "BiliDownloadError",
    "BiliUnsupported",
    "VideoInfo",
    "Quality",
    "DownloadOutcome",
}


def __getattr__(name: str):
    """让 ``from Extension import BiliClient`` 在真正用到时才加载上游。"""
    if name == "bilibili":
        import importlib

        return importlib.import_module(f"{__name__}.bilibilidownloadtool")
    if name in _BILIBILI_EXPORTS:
        from . import bilibilidownloadtool

        return getattr(bilibilidownloadtool, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
