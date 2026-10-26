"""方法库（Method.json）读写 + 三个维护工具。

旧路径：``Agent/history/method.json``
新路径：``data/methods.json``（``settings.paths.data_file("methods.json")``）

首次运行如果没有新文件，会**只读**回退到旧的 ``Agent/history/method.json``；
一旦发生增删改，就写到新路径，旧文件保持不动。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from config.paths import AppPaths
from .registry import record_tool_call, settings_of, tool

logger = logging.getLogger("QQBotAgent.Methods")

DEFAULT_METHOD = {
    "data": [
        {"index": 0, "context": "Method的修改只有单条的增删改"}
    ]
}


def _default_settings() -> Any:
    try:
        from config import load_settings

        return load_settings(auto_create=False)
    except Exception:
        return None


def _legacy_candidates(settings: Any = None) -> list[Path]:
    """旧的 method.json 可能在哪。只读，不写。"""
    paths: list[Path] = []
    env_path = str(os.environ.get("JJ_LEGACY_METHOD_PATH") or "").strip()
    if env_path:
        paths.append(Path(env_path))
    root = None
    if settings is not None:
        root = getattr(getattr(settings, "paths", None), "root", None)
    if root is None:
        root = AppPaths.discover().root
    paths.append(Path(root) / "Agent" / "history" / "method.json")
    return paths


class MethodLibrary:
    """方法库读写（旧类名 ``rw_tools`` 保留为别名）。

    数据结构与旧版一致::

        {"data": [{"index": 0, "context": "..."}, ...]}
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        path: str | Path | None = None,
        legacy_paths: Optional[list[str | Path]] = None,
    ):
        self.settings = settings
        if path is not None:
            self.path = Path(path)
        elif settings is not None and getattr(settings, "paths", None) is not None:
            self.path = Path(settings.paths.data_file("methods.json"))
        else:
            self.path = Path(AppPaths.discover().data_file("methods.json"))
        self.legacy_paths = (
            [Path(item) for item in legacy_paths]
            if legacy_paths is not None
            else _legacy_candidates(settings)
        )
        self.read_only_fallback = False
        self.method = self.read_method()

    # ── 读写 ──
    def write_method(self, data: dict) -> None:
        self.write(self.path, data)

    def write(self, file_path: str | Path, data: dict) -> None:
        path = Path(file_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - 磁盘错误
            logger.error(f"写入方法库失败 {path}: {exc}")

    def read_method(self) -> dict:
        data = self.read(self.path)
        if self.read_only_fallback:
            return data
        return data

    def read(self, file_path: str | Path) -> dict:
        path = Path(file_path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._normalize(data)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"读取方法库失败 {path}: {exc}")
            return {"data": []}

        # 新路径没有 → 只读回退旧文件（找到了不写回，等真正修改时再写新路径）
        for legacy in self.legacy_paths:
            if legacy and legacy.exists() and legacy.resolve() != path.resolve():
                try:
                    with open(legacy, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.read_only_fallback = True
                    logger.info(f"方法库回退读取旧文件: {legacy}")
                    return self._normalize(data)
                except Exception as exc:
                    logger.warning(f"读取旧方法库失败 {legacy}: {exc}")

        # 两边都没有：用旧代码里的内置默认值，等第一次修改时落盘
        return json.loads(json.dumps(DEFAULT_METHOD, ensure_ascii=False))

    @staticmethod
    def _normalize(data: Any) -> dict:
        if not isinstance(data, dict):
            return {"data": []}
        if not isinstance(data.get("data"), list):
            data = {**data, "data": []}
        return data

    # ── 增删改（旧方法名，包括 apeend 的拼写）────────────────
    def apeend_method(self, context: str) -> None:
        index = len(self.method.get("data") or [])
        self.method.setdefault("data", []).append({"index": index, "context": context})
        self.write_method(self.method)

    def alter_method(self, index, context) -> None:
        self.method["data"][index] = {"index": index, "context": context}
        self.write_method(self.method)

    def delete_method(self, index) -> None:
        self.method["data"][index] = {"index": index, "context": "[词条已弃用]"}
        self.write_method(self.method)


# 旧类名（测试 / 外部代码可能直接构造）
rw_tools = MethodLibrary


def build_method_tools(agent: Any) -> list:
    """方法库维护工具：add_Method / delete_Method / alter_Method。"""

    def _library() -> Any:
        library = getattr(agent, "rw_tool", None)
        if library is None:
            library = getattr(agent, "_methods", None)
        return library

    @tool
    async def add_Method(context) -> dict:
        """向Method.json中添加内容
        参数 context:str，需要添加的方法论文本
        返回的是修改后的结果
        """
        logger.info(
            f"[工具调用] add_Method(context='{context[:60]}{'...' if len(context)>60 else ''}')"
        )
        library = _library()
        if library is None:
            logger.warning("[工具] add_Method 失败 - rw_tool 为空")
            record_tool_call("add_Method", {"context": context}, error="rw_tool 为空")
            return {"info": "添加失败"}
        library.apeend_method(context)
        result = library.method["data"][-1]
        logger.info(f"[工具结果] add_Method -> 已添加 index={result['index']}")
        record_tool_call("add_Method", {"context": context}, result=result)
        return result

    @tool
    async def delete_Method(index):
        """依照index将Method的某条内容改为已弃用
        参数 index:int要弃用的索引号
        """
        logger.info(f"[工具调用] delete_Method(index={index})")
        library = _library()
        if library is None:
            logger.warning("[工具] delete_Method 失败 - rw_tool 为空")
            record_tool_call("delete_Method", {"index": index}, error="rw_tool 为空")
            return {"info": "失败"}
        library.delete_method(index)
        result = library.method["data"][index]
        logger.info(f"[工具结果] delete_Method -> 已弃用 index={index}")
        record_tool_call(
            "delete_Method", {"index": index}, result=f"已弃用 index={index}"
        )
        return result

    @tool
    async def alter_Method(index, context):
        """依照index修改Method的某条内容
        参数 index:int 要修改的索引号，
        context:str 修改后的内容
        """
        logger.info(
            f"[工具调用] alter_Method(index={index}, context='{context[:60]}{'...' if len(context)>60 else ''}')"
        )
        library = _library()
        if library is None:
            logger.warning("[工具] alter_Method 失败 - rw_tool 为空")
            record_tool_call(
                "alter_Method",
                {"index": index, "context": context},
                error="rw_tool 为空",
            )
            return {"info": "失败"}
        library.alter_method(index, context)
        result = library.method["data"][index]
        logger.info(f"[工具结果] alter_Method -> 已修改 index={index}")
        record_tool_call(
            "alter_Method", {"index": index, "context": context}, result=result
        )

    return [add_Method, delete_Method, alter_Method]


__all__ = ["DEFAULT_METHOD", "MethodLibrary", "build_method_tools", "rw_tools"]
