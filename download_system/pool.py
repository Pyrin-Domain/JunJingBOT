"""下载池：队列 + 并发 + 重试 + 超时 + 取消。

为什么需要池：
- B 站视频下载/合并很重，不能每条消息都无限制并发；
- 下载失败要重试，但不能无限重试；
- 进程退出或任务取消时，临时文件必须清理。

用法：

    pool = DownloadPool(storage=DownloadStorage(tmp, out), workers=2)
    pool.register(BilibiliProvider())
    result = await pool.submit(DownloadTask(kind="bilibili", source=url))
    await pool.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from typing import Protocol

from .storage import DownloadStorage
from .task import DownloadResult, DownloadStatus, DownloadTask


class DownloadProvider(Protocol):
    kind: str

    async def download(self, task: DownloadTask, storage: DownloadStorage) -> DownloadResult:
        ...


class DownloadError(RuntimeError):
    """所有 provider 失败时抛出的统一异常。"""


class DownloadCancelled(DownloadError):
    pass


class DownloadPool:
    def __init__(
        self,
        *,
        storage: DownloadStorage,
        workers: int = 2,
        max_queue: int = 32,
        task_timeout: float = 600.0,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
    ):
        self.storage = storage
        self.workers = max(1, int(workers))
        self.max_queue = max(1, int(max_queue))
        self.task_timeout = float(task_timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self._providers: dict[str, DownloadProvider] = {}
        self._worker_tasks: list[asyncio.Task] = []
        self._started = False
        self._stopping = False
        self._tasks: dict[str, DownloadTask] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._stats: Counter = Counter()

    # ── 生命周期 ──
    async def start(self) -> "DownloadPool":
        if self._started:
            return self
        self._started = True
        self._stopping = False
        self._worker_tasks = [
            asyncio.create_task(self._worker(index), name=f"download-worker-{index}")
            for index in range(self.workers)
        ]
        return self

    async def stop(self, *, timeout: float = 10.0) -> None:
        if not self._started:
            return
        self._stopping = True
        for _ in self._worker_tasks:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
        if self._worker_tasks:
            done, pending = await asyncio.wait(self._worker_tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._worker_tasks.clear()
        self._started = False

    # ── provider ──
    def register(self, provider: DownloadProvider) -> None:
        self._providers[str(provider.kind)] = provider

    def providers(self) -> list[str]:
        return sorted(self._providers)

    # ── 提交 ──
    async def submit(self, task: DownloadTask) -> DownloadResult:
        if self._stopping:
            raise DownloadError("下载池正在关闭")
        if not self._started:
            await self.start()
        if task.kind not in self._providers:
            raise DownloadError(f"没有注册下载源：{task.kind!r}")
        if task.cancel_requested:
            task.mark_cancelled()
            raise DownloadCancelled("任务已取消")

        self._tasks[task.id] = task
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DownloadResult] = loop.create_future()
        await self._queue.put((task, future))
        self._stats["queued"] += 1

        try:
            return await future
        except asyncio.CancelledError:
            task.cancel_requested = True
            task.mark_cancelled()
            # 如果 provider 已经在跑，连它一起取消；否则下载会在后台继续。
            self.cancel(task.id)
            raise
        finally:
            # 成功/失败都从活跃表移除（保留最近统计即可）
            self._tasks.pop(task.id, None)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel_requested = True
        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
        return True

    # ── worker ──
    async def _worker(self, index: int) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            task, future = item
            try:
                if task.cancel_requested:
                    task.mark_cancelled()
                    if not future.done():
                        future.set_exception(DownloadCancelled("任务已取消"))
                    continue
                if future.cancelled():
                    task.cancel_requested = True
                    task.mark_cancelled()
                    continue
                result = await self._run(task)
                if not future.done():
                    future.set_result(result)
                self._stats["succeeded"] += 1
            except DownloadCancelled as exc:
                task.mark_cancelled()
                self._stats["cancelled"] += 1
                if not future.done():
                    future.set_exception(exc)
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮 worker
                task.mark_failed(str(exc))
                self._stats["failed"] += 1
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.storage.cleanup(task)
                self._queue.task_done()

    async def _run(self, task: DownloadTask) -> DownloadResult:
        provider = self._providers[task.kind]
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if task.cancel_requested:
                raise DownloadCancelled("任务已取消")
            task.mark_running()
            self._stats["running"] += 1
            provider_task = asyncio.create_task(
                provider.download(task, self.storage),
                name=f"download-{task.kind}-{task.id}",
            )
            self._running[task.id] = provider_task
            try:
                result = await asyncio.wait_for(
                    provider_task, timeout=self.task_timeout
                )
                task.mark_succeeded(result)
                return result
            except asyncio.CancelledError:
                task.mark_cancelled()
                current = asyncio.current_task()
                # provider 被 cancel()/超时取消 -> 变成普通失败，别把 worker 一起带走
                if current is not None and getattr(current, "cancelling", lambda: 0)():
                    raise
                raise DownloadCancelled("任务已取消")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                task.error = str(exc)
                if attempt < self.max_retries and not task.cancel_requested:
                    await asyncio.sleep(self.retry_backoff * (attempt + 1))
                    continue
                break
            finally:
                self._running.pop(task.id, None)
                self._stats["running"] = max(0, self._stats["running"] - 1)
        if task.cancel_requested:
            raise DownloadCancelled("任务已取消")
        raise DownloadError(str(last_error) if last_error else "下载失败")

    # ── 状态 ──
    def stats(self) -> dict:
        return {
            "workers": self.workers,
            "started": self._started,
            "queue_size": self._queue.qsize(),
            "active": len(self._tasks),
            "providers": self.providers(),
            "counters": dict(self._stats),
        }

    async def __aenter__(self) -> "DownloadPool":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()


__all__ = [
    "DownloadProvider",
    "DownloadPool",
    "DownloadError",
    "DownloadCancelled",
]
