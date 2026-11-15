"""工具公共设施：并发隔离的 contextvars、调用缓存、可选依赖的取用。

旧版把这些都写在 ``LanguageModule`` 模块顶部；拆出来后：

- ``current_tool_calls``：本轮的副作用记录（用于「别重复发送」「有没有真的回复」）；
- ``current_tool_cache``：本轮工具结果缓存（同一个 OCR 不重复识别）；
- ``current_user_text``：本轮用户原话（供 ``send_dress`` 判断「只是在聊女装」）；
- ``current_images``：本轮可访问的图片 URL（模型只传 source + index）。

全部按 async Task 隔离，防止并发 chat() 互相污染。
"""

from __future__ import annotations

import contextvars
import inspect
import json
from typing import Any, Callable

try:  # pragma: no cover - 有 langchain 时走这里
    from langchain_core.tools import tool as langchain_tool
except Exception:  # pragma: no cover - 没装 langchain 也要能 import
    langchain_tool = None

# 按 async Task 隔离的工具调用记录，防止并发 chat() 互相污染。
#
# **默认值必须是可变容器，不能是 None**：langchain 的 ``BaseTool.arun`` 用
# ``coro_with_context`` 在**复制出来的 context** 里跑工具，工具里
# ``ContextVar.set()`` 的新值传不回调用方（实测：ainvoke 之后外面读到的还是
# 旧值）。所以工具只能靠「原地改一个已经存在的对象」把结果传出去——
# 容器必须由发起这一轮的人（runtime 每轮 set 一个新的）先准备好。
#
# 正因为如此，这里的默认值是**进程内共享**的容器：只有在没人 set 过的时候
# 才会用到它（单测直接调工具）。生产路径每轮都换新的，不会跨轮泄漏。
_current_tool_calls_var: contextvars.ContextVar[list[dict]] = contextvars.ContextVar(
    "current_tool_calls", default=[]
)
_current_tool_cache_var: contextvars.ContextVar[dict[tuple[str, str], Any]] = contextvars.ContextVar(
    "current_tool_cache", default={}
)
# 本轮用户原话（供工具做意图校验，例如「只是聊女装」时不要发照片）
_current_user_text_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_text", default=""
)
# 本轮工具可访问的图片：current=当前消息，quote=被引用/回复的消息，
# forward=合并转发卡片里的图（read_forward 展开后才有）。
# URL 由服务端持有，模型只传 source + index，避免复述 QQ 的长签名 URL 时抄错。
_current_images_var: contextvars.ContextVar[dict[str, list[str]]] = contextvars.ContextVar(
    "current_images", default={}
)
# 本轮 ChatContext：关系工具需要知道当前群号 / 说话人，但工具签名里不传这些。
_current_context_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "current_context", default=None
)

_MISSING = object()


# ── 可选依赖（typed 鸭子类型）────────────────────────────────
def api_of(agent: Any) -> Any:
    """NapCat API：新名字 api，兼容旧名字 napcat_api。"""
    return getattr(agent, "api", None) or getattr(agent, "napcat_api", None)


def messaging_of(agent: Any) -> Any:
    """MessageProcessor：新名字 messaging，兼容旧名字 mp。"""
    return getattr(agent, "messaging", None) or getattr(agent, "mp", None)


def media_of(agent: Any) -> Any:
    """媒体服务：新名字 media，兼容旧测试里的 extension。"""
    return getattr(agent, "media", None) or getattr(agent, "extension", None)


def settings_of(agent: Any) -> Any:
    value = getattr(agent, "settings", None)
    if value is not None:
        return value
    getter = getattr(agent, "_default_settings", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


# ── 工具调用记录 / 缓存 ──────────────────────────────────────
def current_tool_calls() -> list[dict]:
    calls = _current_tool_calls_var.get()
    return calls if isinstance(calls, list) else []


def current_context() -> Any:
    """本轮的 ChatContext（没有则 None）；关系类工具用它取群号和默认对象。"""
    return _current_context_var.get()


def reset_turn_state() -> None:
    """每轮开始：清空工具调用记录与缓存。"""
    _current_tool_calls_var.set([])
    _current_tool_cache_var.set({})


def make_tool_cache_key(tool_name: str, args: dict) -> tuple[str, str]:
    payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return (tool_name, payload)


def get_cached_tool_result(tool_name: str, args: dict) -> Any:
    cache = _current_tool_cache_var.get()
    if not isinstance(cache, dict):
        return _MISSING
    return cache.get(make_tool_cache_key(tool_name, args), _MISSING)


def set_cached_tool_result(tool_name: str, args: dict, result: Any) -> None:
    cache = _current_tool_cache_var.get()
    if not isinstance(cache, dict):
        cache = {}
        _current_tool_cache_var.set(cache)
    cache[make_tool_cache_key(tool_name, args)] = result


def record_tool_call(
    tool_name: str, args: dict, result: Any = None, error: Any = None
) -> None:
    """将工具调用记录到当前 async Task 的上下文变量中，实现并发隔离。"""
    calls = _current_tool_calls_var.get()
    if not isinstance(calls, list):
        calls = []
        _current_tool_calls_var.set(calls)
    entry: dict[str, Any] = {"tool": tool_name, "args": args}
    if result is not None:
        entry["result"] = result
    if error is not None:
        entry["error"] = error
    calls.append(entry)


# ── @tool 装饰器（langchain 缺失时的降级实现）────────────────
class _SimpleTool:
    """langchain 没装时的最小替代：保留 name / description / ainvoke。"""

    def __init__(self, func: Callable[..., Any], *, return_direct: bool = False):
        self.func = func
        self.name = getattr(func, "__name__", "tool")
        self.description = (getattr(func, "__doc__", "") or "").strip()
        self.return_direct = return_direct

    async def ainvoke(self, args: dict | None = None) -> Any:
        result = self.func(**(args or {}))
        if inspect.isawaitable(result):
            result = await result
        return result

    async def invoke(self, args: dict | None = None) -> Any:
        return await self.ainvoke(args)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SimpleTool {self.name}>"


def tool(func: Callable[..., Any] | None = None, **kwargs: Any):
    """统一的 ``@tool``：有 langchain 用 langchain，没有就退回 ``_SimpleTool``。"""
    if langchain_tool is not None:
        return langchain_tool(func, **kwargs) if func is not None else langchain_tool(**kwargs)

    def _wrap(fn: Callable[..., Any]):
        return _SimpleTool(fn, return_direct=bool(kwargs.get("return_direct")))

    return _wrap


__all__ = [
    "_MISSING",
    "_current_images_var",
    "_current_tool_cache_var",
    "_current_tool_calls_var",
    "_current_user_text_var",
    "_current_context_var",
    "api_of",
    "current_context",
    "current_tool_calls",
    "get_cached_tool_result",
    "make_tool_cache_key",
    "media_of",
    "messaging_of",
    "record_tool_call",
    "reset_turn_state",
    "set_cached_tool_result",
    "settings_of",
    "tool",
]
