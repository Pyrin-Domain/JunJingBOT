"""异步小工具：后台任务、取消、超时。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any


def spawn(coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task:
    """创建后台任务，保留引用避免被 GC。"""
    task = asyncio.create_task(coro, name=name)
    # asyncio 只弱引用 task；挂到当前 loop 上，防止运行中被回收
    try:
        loop = asyncio.get_running_loop()
        bucket = getattr(loop, "_jj_background_tasks", None)
        if bucket is None:
            bucket = set()
            setattr(loop, "_jj_background_tasks", bucket)
        bucket.add(task)
        task.add_done_callback(bucket.discard)
    except RuntimeError:
        pass
    return task


async def cancel(task: asyncio.Task | None, *, timeout: float = 3.0) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
        await asyncio.wait_for(task, timeout=timeout)


async def cancel_all(tasks: list[asyncio.Task], *, timeout: float = 3.0) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait(tasks, timeout=timeout)


__all__ = ["spawn", "cancel", "cancel_all"]
