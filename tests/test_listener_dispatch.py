"""listen() 与事件分发器的集成：过滤、顺序、并发和停机。"""

from __future__ import annotations

import asyncio

import pytest

from transport.listener import listen


class _Config:
    backend_label = "测试后端"
    mode_label = "反向(ws_re)"


class _FakeConnection:
    def __init__(self):
        self.config = _Config()
        self.handler = None
        self.connected = asyncio.Event()

    def set_event_handler(self, handler):
        self.handler = handler
        return self

    async def wait_for_connect(self):
        await self.connected.wait()
        return True


async def _start_listener(handler, **kwargs):
    conn = _FakeConnection()
    conn.connected.set()
    task = asyncio.create_task(
        listen({}, handler, conn=conn, **kwargs),
        name="test-listener",
    )
    await asyncio.sleep(0)
    assert conn.handler is not None
    return conn, task


async def _stop_listener(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _group_event(group_id: int, message_id: int) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": 10001,
        "message_id": message_id,
    }


@pytest.mark.asyncio
async def test_non_message_events_are_ignored():
    handled = []

    async def handler(event):
        handled.append(event)

    conn, task = await _start_listener(handler)
    conn.handler({"post_type": "notice", "notice_type": "group_upload"})
    conn.handler(
        {
            "post_type": "message_sent",
            "message_type": "group",
            "group_id": 1,
            "message_id": 2,
        }
    )
    await asyncio.sleep(0)

    assert handled == []
    await _stop_listener(task)


@pytest.mark.asyncio
async def test_same_group_messages_are_fifo():
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    handled = []

    async def handler(event):
        handled.append(event["message_id"])
        if event["message_id"] == 1:
            first_started.set()
            await release_first.wait()

    conn, task = await _start_listener(handler, max_concurrency=2, max_pending=10)
    conn.handler(_group_event(10, 1))
    conn.handler(_group_event(10, 2))
    await first_started.wait()
    await asyncio.sleep(0.01)
    assert handled == [1]

    release_first.set()
    for _ in range(20):
        if handled == [1, 2]:
            break
        await asyncio.sleep(0.01)
    assert handled == [1, 2]
    await _stop_listener(task)


@pytest.mark.asyncio
async def test_different_groups_run_in_parallel():
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = set()

    async def handler(event):
        started.add(event["group_id"])
        if len(started) == 2:
            both_started.set()
        await release.wait()

    conn, task = await _start_listener(handler, max_concurrency=2, max_pending=10)
    conn.handler(_group_event(1, 101))
    conn.handler(_group_event(2, 102))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started == {1, 2}
    release.set()
    await _stop_listener(task)


@pytest.mark.asyncio
async def test_listener_cancel_cancels_active_handler():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_event):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    conn, task = await _start_listener(handler)
    conn.handler(_group_event(1, 1))
    await started.wait()

    await _stop_listener(task)

    assert task.cancelled()
    assert cancelled.is_set()
    remaining = [
        pending
        for pending in asyncio.all_tasks()
        if pending is not asyncio.current_task() and "onebot-session-" in pending.get_name()
    ]
    assert remaining == []


@pytest.mark.asyncio
async def test_listener_passes_capacity_limits():
    release = asyncio.Event()
    first_started = asyncio.Event()
    handled = []

    async def handler(event):
        handled.append(event["message_id"])
        if event["message_id"] == 1:
            first_started.set()
            await release.wait()

    conn, task = await _start_listener(handler, max_concurrency=1, max_pending=2)
    conn.handler(_group_event(1, 1))
    await first_started.wait()
    conn.handler(_group_event(2, 2))
    conn.handler(_group_event(3, 3))
    await asyncio.sleep(0.01)

    assert handled == [1]
    release.set()
    for _ in range(20):
        if len(handled) == 2:
            break
        await asyncio.sleep(0.01)
    assert handled == [1, 2]
    await _stop_listener(task)
