"""下载目录与临时文件管理。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .task import DownloadTask


class DownloadStorage:
    def __init__(self, temp_dir: str | Path, output_dir: str | Path):
        self.temp_dir = Path(temp_dir)
        self.output_dir = Path(output_dir)
        self.ensure()

    def ensure(self) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task: DownloadTask) -> Path:
        path = self.temp_dir / task.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def temp_file(self, task: DownloadTask, name: str) -> Path:
        return self.task_dir(task) / Path(name).name

    def output_file(self, task: DownloadTask, name: str) -> Path:
        target = self.output_dir / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def cleanup(self, task: DownloadTask) -> None:
        shutil.rmtree(self.temp_dir / task.id, ignore_errors=True)

    def cleanup_stale(self, *, max_age_seconds: float = 24 * 3600) -> int:
        """删除超过 max_age 的临时目录（进程崩溃留下的）。"""
        if not self.temp_dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        for item in self.temp_dir.iterdir():
            if not item.is_dir():
                continue
            try:
                age = now - item.stat().st_mtime
            except OSError:
                continue
            if age > max_age_seconds:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed


__all__ = ["DownloadStorage"]
