"""DownloadPool：成功、重试、失败、超时、取消与临时目录清理。"""

import asyncio

import pytest

from download_system.pool import DownloadCancelled, DownloadError, DownloadPool
from download_system.storage import DownloadStorage
from download_system.task import DownloadResult, DownloadStatus, DownloadTask


def _pool(tmp_path, **kwargs):
    storage = DownloadStorage(tmp_path / "tmp", tmp_path / "out")
    return DownloadPool(storage=storage, **kwargs), storage


def test_successful_download_and_cleanup(tmp_path):
    async def run():
        pool, storage = _pool(tmp_path, workers=1)

        class Provider:
            kind = "fake"

            async def download(self, task, storage):
                (storage.task_dir(task) / "part.bin").write_bytes(b"abc")
                return DownloadResult(path=storage.output_file(task, "a.mp4"), size=3)

        pool.register(Provider())
        task = DownloadTask(kind="fake", source="x")
        result = await pool.submit(task)
        temp_left = list((tmp_path / "tmp").iterdir())
        stats = pool.stats()
        await pool.stop()
        return task, result, temp_left, stats

    task, result, temp_left, stats = asyncio.run(run())
    assert result.size == 3
    assert task.status is DownloadStatus.SUCCEEDED
    assert temp_left == []
    assert stats["counters"]["succeeded"] == 1


def test_retries_then_succeeds(tmp_path):
    async def run():
        pool, _ = _pool(tmp_path, workers=1, max_retries=2, retry_backoff=0)

        class Flaky:
            kind = "flaky"
            calls = 0

            async def download(self, task, storage):
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("boom")
                return DownloadResult(path=None, size=1)

        provider = Flaky()
        pool.register(provider)
        task = DownloadTask(kind="flaky", source="x")
        result = await pool.submit(task)
        await pool.stop()
        return task, result, provider.calls

    task, result, calls = asyncio.run(run())
    assert calls == 3
    assert task.attempts == 3
    assert task.status is DownloadStatus.SUCCEEDED
    assert result.size == 1


def test_failure_marks_task_and_raises(tmp_path):
    async def run():
        pool, _ = _pool(tmp_path, workers=1, max_retries=1, retry_backoff=0)

        class Broken:
            kind = "broken"

            async def download(self, task, storage):
                raise ValueError("nope")

        pool.register(Broken())
        task = DownloadTask(kind="broken", source="x")
        with pytest.raises(DownloadError):
            await pool.submit(task)
        await pool.stop()
        return task

    task = asyncio.run(run())
    assert task.status is DownloadStatus.FAILED
    assert "nope" in (task.error or "")


def test_timeout_is_reported_as_failure(tmp_path):
    async def run():
        pool, _ = _pool(tmp_path, workers=1, max_retries=0, task_timeout=0.05)

        class Slow:
            kind = "slow"

            async def download(self, task, storage):
                await asyncio.sleep(5)

        pool.register(Slow())
        task = DownloadTask(kind="slow", source="x")
        with pytest.raises(DownloadError):
            await pool.submit(task)
        await pool.stop()
        return task

    task = asyncio.run(run())
    assert task.status is DownloadStatus.FAILED


def test_cancel_running_task(tmp_path):
    async def run():
        pool, _ = _pool(tmp_path, workers=1, max_retries=0)
        started = asyncio.Event()

        class Blocking:
            kind = "blocking"

            async def download(self, task, storage):
                started.set()
                await asyncio.sleep(30)

        pool.register(Blocking())
        task = DownloadTask(kind="blocking", source="x")
        consumer = asyncio.create_task(pool.submit(task))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert pool.cancel(task.id) is True
        with pytest.raises(DownloadCancelled):
            await asyncio.wait_for(consumer, timeout=2)
        await pool.stop()
        return task

    task = asyncio.run(run())
    assert task.status is DownloadStatus.CANCELLED


def test_unknown_provider_is_rejected(tmp_path):
    async def run():
        pool, _ = _pool(tmp_path, workers=1)
        with pytest.raises(DownloadError):
            await pool.submit(DownloadTask(kind="missing", source="x"))
        await pool.stop()

    asyncio.run(run())
