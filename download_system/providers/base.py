"""下载源接口。

新增一个下载源只需要：

1. 在 ``download_system/providers/`` 下建一个类，``kind`` 唯一；
2. 实现 ``async def download(task, storage) -> DownloadResult``；
3. 在 ``DownloadService`` 里注册。

Provider 只负责“把一个 URL/关键字变成文件”，不负责发消息、不读业务配置。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..storage import DownloadStorage
from ..task import DownloadResult, DownloadTask


class BaseProvider(ABC):
    kind: str = ""

    @abstractmethod
    async def download(
        self, task: DownloadTask, storage: DownloadStorage
    ) -> DownloadResult:
        raise NotImplementedError

    # ── 给子类用的小工具 ──
    @staticmethod
    def output_name(task: DownloadTask, default: str) -> str:
        name = str(task.options.get("filename") or default).strip()
        return name or default


__all__ = ["BaseProvider"]
