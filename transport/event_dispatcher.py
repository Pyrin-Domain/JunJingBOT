"""按会话串行、全局受限的轻量事件分发器。

OneBot 的 reader 不能等待业务处理，否则一个慢模型调用就会堵住 socket。
同时，同一群 / 私聊的消息并发处理会打乱上下文和回复顺序。这个分发器只为
每个会话保留一个 worker，不同会话之间并行，并限制总积压量。
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Callable, Hashable
from typing import Any

from core.logging import get_logger

log = get_logger("transport.event_dispatcher")

DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_PENDING = 256


class EventDispatcher:
    """把事件按会话排队，避免同会话乱序和无界后台任务。"""

    def __init__(
        self,
        handler: Callable[[dict], Any],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_pending: int = DEFAULT_MAX_PENDING,
    ):
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handler = handler
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_pending = max(1, int(max_pending))

        self._queues: dict[Hashable, deque[dict]] = {}
        self._ready: deque[Hashable] = deque()
        self._workers: dict[Hashable, asyncio.Task] = {}
        self._outstanding = 0
        self._closed = False
        self._overload_logged = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def pending_count(self) -> int:
        """已提交但尚未处理完的事件数（包含正在处理的事件）。"""
        return self._outstanding

    @property
    def running_count(self) -> int:
        return len(self._workers)

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(self, event: dict) -> bool:
        """非阻塞提交事件；关闭或达到积压上限时返回 ``False``。"""
        if self._closed or not isinstance(event, dict):
            return False
        if self._outstanding < self.max_pending:
            self._overload_logged = False
        if self._outstanding >= self.max_pending:
            if not self._overload_logged:
                log.warning(
                    "[事件] 积压达到上限 %d，丢弃新事件 %s",
                    self.max_pending,
                    event.get("message_id", "?"),
                )
                self._overload_logged = True
            return False

        key = self.session_key(event)
        queue = self._queues.get(key)
        if queue is None:
            queue = deque()
            self._queues[key] = queue
        queue.append(event)
        self._outstanding += 1
        self._idle.clear()

        if key not in self._workers and key not in self._ready:
            self._ready.append(key)
        self._schedule()
        return True

    async def join(self, timeout: float | None = None) -> bool:
        """等待当前已提交事件处理完毕；超时返回 ``False``。"""
        if self._outstanding == 0:
            return True
        if timeout is None:
            await self._idle.wait()
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def close(self) -> None:
        """拒绝新事件、取消所有 worker，并等待取消完成。"""
        self._closed = True
        self._ready.clear()
        for queue in self._queues.values():
            self._outstanding -= len(queue)
            queue.clear()
        self._queues.clear()
        self._mark_idle_if_empty()

        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._mark_idle_if_empty()

    @staticmethod
    def session_key(event: dict) -> Hashable:
        """群按 group_id、私聊按 user_id 分道；缺字段时用事件标识兜底。"""
        message_type = str(event.get("message_type") or "")
        if message_type == "group" and event.get("group_id") is not None:
            return ("group", str(event["group_id"]))
        if message_type == "private" and event.get("user_id") is not None:
            return ("private", str(event["user_id"]))
        return (
            "event",
            str(event.get("post_type") or ""),
            message_type,
            str(
                event.get("message_id")
                or event.get("notice_type")
                or event.get("request_type")
                or id(event)
            ),
        )

    def _schedule(self) -> None:
        while (
            not self._closed
            and len(self._workers) < self.max_concurrency
            and self._ready
        ):
            key = self._ready.popleft()
            if key in self._workers or not self._queues.get(key):
                continue
            task = asyncio.create_task(
                self._run_session(key),
                name=f"onebot-session-{key[0]}-{key[1]}",
            )
            self._workers[key] = task

    async def _run_session(self, key: Hashable) -> None:
        active = False
        try:
            while not self._closed:
                queue = self._queues.get(key)
                if not queue:
                    break
                event = queue.popleft()
                active = True
                try:
                    await self._call_handler(event)
                finally:
                    self._outstanding -= 1
                    active = False
                    self._mark_idle_if_empty()
        finally:
            if active:
                self._outstanding -= 1
            self._workers.pop(key, None)
            queue = self._queues.get(key)
            if queue:
                self._ready.appendleft(key)
            else:
                self._queues.pop(key, None)
            self._mark_idle_if_empty()
            self._schedule()

    async def _call_handler(self, event: dict) -> None:
        try:
            result = self._handler(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条失败不能带走会话 worker
            log.error(
                "[事件] 会话 %s 处理消息 %s 失败",
                self.session_key(event),
                event.get("message_id", "?"),
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _mark_idle_if_empty(self) -> None:
        if self._outstanding <= 0:
            self._outstanding = 0
            self._idle.set()


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_PENDING",
    "EventDispatcher",
]
