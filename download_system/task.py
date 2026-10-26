"""下载任务与结果。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadResult:
    """一次下载的产物。"""

    path: Path | None = None
    size: int = 0
    mime: str = ""
    url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def exists(self) -> bool:
        return bool(self.path and self.path.is_file())


@dataclass
class DownloadTask:
    """一个下载请求。

    ``options`` 是给 provider 的私有参数（例如输出目录、画质、文件名前缀），
    pool 不解释它。
    """

    kind: str
    source: str
    options: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: DownloadStatus = DownloadStatus.QUEUED
    result: DownloadResult | None = None
    error: str | None = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False

    # ── 状态 ──
    def mark_running(self) -> None:
        self.status = DownloadStatus.RUNNING
        self.attempts += 1
        self.started_at = time.time()
        self.error = None

    def mark_succeeded(self, result: DownloadResult) -> None:
        self.status = DownloadStatus.SUCCEEDED
        self.result = result
        self.finished_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = DownloadStatus.FAILED
        self.error = str(error)
        self.finished_at = time.time()

    def mark_cancelled(self) -> None:
        self.status = DownloadStatus.CANCELLED
        self.error = "cancelled"
        self.finished_at = time.time()

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "status": self.status.value,
            "attempts": self.attempts,
            "error": self.error,
            "duration": self.duration,
            "result": (
                {
                    "path": str(self.result.path) if self.result.path else "",
                    "size": self.result.size,
                    "mime": self.result.mime,
                }
                if self.result
                else None
            ),
        }


__all__ = ["DownloadStatus", "DownloadTask", "DownloadResult"]
