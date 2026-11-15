"""事件分发器：同会话顺序、跨会话并发、容量和停机行为。"""

from __future__ import annotations

import asyncio

import pytest

from transport.event_dispatcher import EventDispatcher


def _event(session: int, message: int, *, message_type: str = "group") -> dict:
    if message_type == "group":
        return {
            "message_type": "group",
            "group_id": session,
            "message_id": message,
        }
    return {
        "message_type": "private",
        "user_id": session,
        "message_id": message,
    }


@pytest.mark.asyncio
async def test_same_session_is_fifo():
    handled: list[int] = []

    async def handler(event):
        if event["message_id"] == 1:
            await asyncio.sleep(0.02)
        handled.append(event["message_id"])

    dispatcher = EventDispatcher(handler, max_concurrency=4, max_pending=10)
    for message_id in (1, 2, 3):
        assert dispatcher.submit(_event(10, message_id))

    assert await dispatcher.join(timeout=1)
    assert handled == [1, 2, 3]
    await dispatcher.close()


@pytest.mark.asyncio
async def test_different_sessions_run_in_parallel():
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[int] = set()

    async def handler(event):
        started.add(event["group_id"])
        if len(started) == 2:
            both_started.set()
        await release.wait()

    dispatcher = EventDispatcher(handler, max_concurrency=2, max_pending=10)
    assert dispatcher.submit(_event(1, 101))
    assert dispatcher.submit(_event(2, 102))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started == {1, 2}
    release.set()
    assert await dispatcher.join(timeout=1)
    await dispatcher.close()


@pytest.mark.asyncio
async def test_global_concurrency_is_limited():
    release = asyncio.Event()
    active = 0
    peak = 0

    async def handler(_event):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    dispatcher = EventDispatcher(handler, max_concurrency=2, max_pending=10)
    for session in range(5):
        assert dispatcher.submit(_event(session, 100 + session))

    await asyncio.sleep(0.02)
    assert peak == 2
    assert dispatcher.running_count == 2
    release.set()
    assert await dispatcher.join(timeout=1)
    await dispatcher.close()


@pytest.mark.asyncio
async def test_pending_limit_rejects_new_events(caplog):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_event):
        started.set()
        await release.wait()

    dispatcher = EventDispatcher(handler, max_concurrency=1, max_pending=2)
    assert dispatcher.submit(_event(1, 1))
    await started.wait()
    assert dispatcher.submit(_event(2, 2))
    assert dispatcher.submit(_event(3, 3)) is False
    assert dispatcher.submit(_event(4, 4)) is False
    assert dispatcher.pending_count == 2
    assert caplog.text.count("积压达到上限") == 1

    release.set()
    assert await dispatcher.join(timeout=1)
    await dispatcher.close()


@pytest.mark.asyncio
async def test_handler_exception_is_logged_and_session_continues(caplog):
    handled: list[int] = []

    async def handler(event):
        if event["message_id"] == 1:
            raise RuntimeError("boom")
        handled.append(event["message_id"])

    dispatcher = EventDispatcher(handler, max_concurrency=1, max_pending=10)
    assert dispatcher.submit(_event(1, 1))
    assert dispatcher.submit(_event(1, 2))

    assert await dispatcher.join(timeout=1)
    assert handled == [2]
    assert "boom" in caplog.text
    await dispatcher.close()


@pytest.mark.asyncio
async def test_close_rejects_new_events_and_cancels_running_work():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_event):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    dispatcher = EventDispatcher(handler, max_concurrency=1, max_pending=10)
    assert dispatcher.submit(_event(1, 1))
    await started.wait()

    await dispatcher.close()

    assert dispatcher.closed
    assert dispatcher.submit(_event(1, 2)) is False
    assert cancelled.is_set()
    assert dispatcher.pending_count == 0
    await dispatcher.close()  # 幂等


@pytest.mark.asyncio
async def test_sync_handler_is_supported():
    handled: list[int] = []

    def handler(event):
        handled.append(event["message_id"])

    dispatcher = EventDispatcher(handler)
    assert dispatcher.submit(_event(1, 1))
    assert await dispatcher.join(timeout=1)
    assert handled == [1]
    await dispatcher.close()


def test_session_key_handles_private_and_unknown_events():
    assert EventDispatcher.session_key(_event(12, 1)) == ("group", "12")
    assert EventDispatcher.session_key(
        _event(34, 2, message_type="private")
    ) == ("private", "34")
    assert EventDispatcher.session_key({"message_id": 56}) == (
        "event",
        "",
        "",
        "56",
    )
