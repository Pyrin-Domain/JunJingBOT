"""JSON 文件读写：原子写入、深合并、安全读取。

所有配置和数据索引都走这里，避免每个模块各写一套 ``open/json.dump``。
"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 ``default`` 的深拷贝。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return deepcopy(default)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[配置] 读取 {path} 失败：{exc}")
        return deepcopy(default)


def write_json(path: str | os.PathLike[str], data: Any, *, indent: int = 2) -> Path:
    """原子写 JSON：先写同目录临时文件，再 ``os.replace``。

    这样面板保存到一半崩溃，也不会留下半个 JSON。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def deep_merge(base: dict, extra: dict | None) -> dict:
    """递归合并：dict 逐层合并，其它类型（含 list）直接覆盖。"""
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


__all__ = ["read_json", "write_json", "deep_merge"]
