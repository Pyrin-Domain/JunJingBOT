"""``utils`` 的轻量替代实现（上游为 ``core/utils.py``）。

上游 ``bilibili_parser.py`` 只在两处惰性引用它：

    from utils import verify_and_ensure_save

原版 ``core/utils.py`` 顶层 ``import PyQt5``，机器人在没有桌面环境的服务器上
会直接 ImportError。因此这里提供**行为等价、但不含任何 Qt 依赖**的实现。

刻意与上游不同的两点：

1. 不做「搬到 ``下载/`` 子目录再复制一份」的兜底 —— 那会让机器人悄悄多出
   一个没人管的目录；这里只在目标无效时尝试一次复制，失败就如实返回 False。
2. 不弹窗、不打日志到 GUI，只写标准 logging。
"""

from __future__ import annotations

import logging
import os
import shutil

__all__ = ["verify_and_ensure_save", "get_unique_filename"]

_logger = logging.getLogger(__name__)


def get_unique_filename(path: str | os.PathLike) -> str:
    """目标已存在时追加 ``_1`` / ``_2`` …，返回不冲突的路径。"""
    path = os.fspath(path)
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{base}_{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def verify_and_ensure_save(
    target_path: str | os.PathLike,
    source_path: str | os.PathLike | None = None,
    content: bytes | None = None,
) -> tuple[str, bool]:
    """确认 ``target_path`` 已落盘且非空；必要时从 ``source_path`` / ``content`` 补写。

    返回 ``(实际路径, 是否成功)``，与上游签名一致。
    """
    if not target_path:
        _logger.error("verify_and_ensure_save: 目标路径为空")
        return "", False

    target = os.fspath(target_path)

    if os.path.exists(target):
        try:
            if os.path.getsize(target) > 0:
                return target, True
        except OSError as exc:
            _logger.warning("校验目标文件失败：%s", exc)
        else:
            _logger.warning("目标文件存在但为空：%s", target)

    if source_path and os.path.exists(source_path):
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copy2(source_path, target)
            if os.path.getsize(target) > 0:
                _logger.info("已从源文件补写：%s", target)
                return target, True
        except OSError as exc:
            _logger.error("从源文件补写失败：%s", exc)

    if content:
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)
            return target, True
        except OSError as exc:
            _logger.error("写入内容失败：%s", exc)

    return target, False
