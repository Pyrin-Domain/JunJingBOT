"""消息处理器的后台协程必须有归属，停机时能全部取消。"""

from __future__ import annotations

import asyncio

import pytest

from messaging.processor import MessageProcessor


def _processor() -> MessageProcessor:
    return MessageProcessor(api=object(), images=object())


@pytest.mark.asyncio
async def test_background_task_failure_is_logged_and_removed(caplog):
    mp = _processor()

    async def fail():
        raise RuntimeError("background exploded")

    task = mp._spawn_background(fail(), name="test-failing-background")
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert task not in mp._background_tasks
    assert "background exploded" in caplog.text
    await mp.close()


@pytest.mark.asyncio
async def test_close_cancels_background_tasks_and_rejects_new_work():
    mp = _processor()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mp._spawn_background(wait_forever(), name="test-wait-forever")
    await started.wait()

    await mp.close()

    assert cancelled.is_set()
    assert mp._background_tasks == set()
    assert mp._spawn_background(wait_forever(), name="rejected") is None
    await mp.close()  # 幂等


@pytest.mark.asyncio
async def test_ocr_images_are_spawned_as_tracked_tasks():
    mp = _processor()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_ocr_and_send(*, group_id, url):
        started.set()
        await release.wait()

    mp.ocr_and_send = fake_ocr_and_send
    event = {
        "message_id": 1,
        "group_id": 2,
        "message_type": "group",
        "user_id": 3,
        "message": [{"type": "reply", "data": {"id": 4}}],
    }

    async def fake_get_message(*, message_id):
        return {
            "data": {
                "message": [
                    {"type": "image", "data": {"url": "https://x/1.jpg"}},
                    {"type": "image", "data": {"url": "https://x/2.jpg"}},
                ]
            }
        }

    mp.nc = type("API", (), {"get_message": staticmethod(fake_get_message)})()
    mp.ensure_connected = lambda: asyncio.sleep(0)

    await mp.ocr(event)
    await started.wait()
    assert len(mp._background_tasks) == 2

    release.set()
    await asyncio.gather(*list(mp._background_tasks))
    assert mp._background_tasks == set()
    await mp.close()
