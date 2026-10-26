"""下载服务门面：业务层只和它打交道。"""

from __future__ import annotations

from pathlib import Path

from .pool import DownloadPool
from .providers.jm import JMProvider
from .storage import DownloadStorage
from .task import DownloadResult, DownloadTask


class DownloadService:
    def __init__(
        self,
        storage: DownloadStorage,
        *,
        workers: int = 2,
        max_queue: int = 32,
        task_timeout: float = 600.0,
        max_retries: int = 2,
        pool: DownloadPool | None = None,
    ):
        self.storage = storage
        self.pool = pool or DownloadPool(
            storage=storage,
            workers=workers,
            max_queue=max_queue,
            task_timeout=task_timeout,
            max_retries=max_retries,
        )
        self._register_defaults()

    def _register_defaults(self) -> None:
        from .providers.bilibili import BilibiliProvider
        from .providers.music import MusicProvider

        self.pool.register(BilibiliProvider())
        self.pool.register(MusicProvider())
        # 占位下载源：只保留接口，download() 抛 JmNotConfigured
        self.pool.register(JMProvider())

    # ── 生命周期 ──
    async def start(self) -> "DownloadService":
        await self.pool.start()
        return self

    async def close(self) -> None:
        await self.pool.stop()

    async def __aenter__(self) -> "DownloadService":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ── 业务入口 ──
    async def download_bilibili(
        self, url: str, *, output_dir: str | Path | None = None, filename: str = ""
    ) -> DownloadResult:
        options: dict = {}
        if output_dir is not None:
            options["output_dir"] = str(output_dir)
        if filename:
            options["filename"] = filename
        return await self.pool.submit(
            DownloadTask(kind="bilibili", source=str(url), options=options)
        )

    async def download_music(self, keyword: str) -> DownloadResult:
        return await self.pool.submit(
            DownloadTask(kind="music", source=str(keyword), options={})
        )

    async def download_jm(self, source: str) -> DownloadResult:
        """占位下载源：返回仓库内的本地占位 jpg，不抓取任何站点。

        保留这个方法是为了让 ``/jm`` 命令只依赖 ``DownloadService``。

        占位图缺失时**不进队列**：否则池子的重试策略会把「文件不存在」
        包装成 ``DownloadError``，调用方还得靠字符串猜原因。
        """
        JMProvider.ensure_ready()
        return await self.pool.submit(
            DownloadTask(kind="jm", source=str(source), options={})
        )

    def stats(self) -> dict:
        return self.pool.stats()


__all__ = ["DownloadService"]
