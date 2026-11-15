"""B 站视频下载历史：按 BV 号索引本地文件，同一条视频不重复下载。

背景：同一个视频会被反复搬（不同群、被回复的卡片、链接重复出现在文本里），
每次都在 ``download_bilibili`` 里重新拉一遍，既慢又吃磁盘
（``data/downloads`` 里已经出现过 ``xxx.mp4`` 和 ``xxx_2.mp4`` 同内容两份）。

这里只在**发送前**做一次「本地有没有」的判断：

* 有记录且文件还在 → 直接发那个文件，跳过下载；
* 有记录但文件被清理 → 顺手删掉这条记录，正常重新下载；
* 没记录 → 下载完立刻落库（**先落库再发送**：即便发送超时，下次也能命中，
  不用再下 100MB）。

单进程 bot 用，读写都走同一个进程，JSON 写入用 ``os.replace`` 保证原子性。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

__all__ = ["BiliHistory", "video_key", "DEFAULT_KEEP"]

EVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AVID_RE = re.compile(r"(?:^|[/?&=])av(\d+)", re.IGNORECASE)

#: 历史最多保留多少条；超出后丢最旧的（文件名里带时间戳）。
DEFAULT_KEEP = 200


def video_key(url: str) -> str:
    """从链接里取历史键：优先 BV 号，只有 av 号时退成 ``av<id>``。

    ``send_bili_video`` 拿到的是 ``resolve_video_url`` 之后的页面链接，
    正常都带 BV 号；短链没解析成功时可能一个都取不到，那就返回空串表示
    「这条不参与历史」。
    """
    text = str(url or "")
    if not text:
        return ""
    match = EVID_RE.search(text)
    if match:
        return match.group(0)
    av = AVID_RE.search(text)
    if av:
        return f"av{av.group(1)}"
    return ""


class BiliHistory:
    """极简 JSON 历史表：``{key: {path, title, size, at}}``。"""

    def __init__(self, path: str | Path, *, keep: int = DEFAULT_KEEP):
        self.path = Path(path)
        self.keep = max(1, int(keep))
        self._data: dict[str, dict] | None = None

    # ── 读写 ─────────────────────────────────────────────────────
    def _load(self) -> dict[str, dict]:
        if self._data is not None:
            return self._data
        raw = {}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        self._data = {
            str(k): v for k, v in (raw or {}).items() if isinstance(v, dict)
        }
        return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[B站卡片] 写下载历史失败: {exc}")

    # ── 查询 / 记录 ───────────────────────────────────────────────
    def get(self, key: str) -> Path | None:
        """命中的本地文件；文件已经不在就删记录并返回 ``None``。"""
        if not key:
            return None
        entry = self._load().get(key)
        if not entry:
            return None
        path = Path(str(entry.get("path") or ""))
        if path.is_file():
            return path
        print(f"[B站卡片] 历史 {key} 的文件已不存在，重新下载: {path}")
        self.forget(key)
        return None

    def remember(self, key: str, path: str | Path, *, title: str = "") -> None:
        if not key:
            return
        target = Path(path)
        data = self._load()
        data[key] = {
            "path": str(target),
            "title": str(title or ""),
            "size": self._size(target),
            "at": time.time(),
        }
        self._trim()
        self._save()

    def forget(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._save()

    def __len__(self) -> int:
        return len(self._load())

    # ── 内部 ─────────────────────────────────────────────────────
    def _trim(self) -> None:
        data = self._load()
        if len(data) <= self.keep:
            return
        ordered = sorted(
            data.items(), key=lambda kv: float(kv[1].get("at") or 0), reverse=True
        )
        keep_keys = {key for key, _ in ordered[: self.keep]}
        for key in [k for k in data if k not in keep_keys]:
            data.pop(key, None)

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
