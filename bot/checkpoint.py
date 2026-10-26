"""断点文件读写（``data/check_point/<user_id>.json``）。

语义与旧版完全一致：``{group_id: event}``，每 10 秒由 MessageProcessor
自己的后台循环落盘。这里只提供读写/快照这几个纯工具，**不**再复制一份
保存循环 —— 运行中的保存只发生在 MessageProcessor 里（它持有内存中的
``check_point``）。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from config.paths import AppPaths
from core.jsonstore import read_json, write_json

__all__ = ["checkpoint_dir", "checkpoint_file", "load", "save", "snapshot"]


def checkpoint_dir(settings) -> Path:
    """断点目录：跟项目布局走（``data/check_point``）。"""
    paths = getattr(settings, "paths", None)
    if paths is not None:
        return Path(paths.checkpoint_dir)
    if isinstance(settings, dict):
        value = settings.get("checkpoint_dir")
        if value:
            return Path(str(value))
    return AppPaths.discover().checkpoint_dir


def checkpoint_file(settings, user_id) -> Path:
    """某个 QQ 号的断点文件。"""
    return checkpoint_dir(settings) / f"{user_id}.json"


def load(settings, user_id) -> dict:
    """读断点；文件不存在时按旧行为创建空文件并返回 ``{}``。

    损坏 / 非 dict 的 JSON 一律当 ``{}``（旧实现同样吞掉异常），
    下一条消息进来会重新填。
    """
    path = checkpoint_file(settings, user_id)
    if not path.exists():
        write_json(path, {}, indent=4)
        return {}
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def save(settings, user_id, data) -> Path:
    """原子写断点（旧文件是 4 空格缩进，保持一致）。"""
    path = checkpoint_file(settings, user_id)
    write_json(path, dict(data or {}), indent=4)
    return path


def snapshot(data) -> dict:
    """深拷贝一份断点，供回放期间遍历时不被后台保存改到。"""
    return deepcopy(data or {})
