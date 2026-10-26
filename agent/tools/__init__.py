"""工具集合：按域拆分，再按旧版顺序组装成 ``QQBotAgent`` 的工具列表。

旧版把 13 个工具全写在 ``LanguageModule._build_tools()`` 里；拆开之后每个
builder 只依赖 ``agent`` 上注入的服务，因此 ``QQBotAgent.__new__`` 的测试
只设置 ``napcat_api`` / ``extension`` / ``mp`` / ``rw_tool`` 也能构建工具。
"""

from __future__ import annotations

from typing import Any

from .chat import build_chat_tools
from .history import build_history_tools
from .media import build_media_tools
from .methods import build_method_tools
from .moderation import build_moderation_tools
from .relations import build_relation_tools
from .registry import (
    _MISSING,
    _current_context_var,
    _current_images_var,
    _current_tool_cache_var,
    _current_tool_calls_var,
    _current_user_text_var,
    api_of,
    current_tool_calls,
    get_cached_tool_result,
    make_tool_cache_key,
    media_of,
    messaging_of,
    record_tool_call,
    reset_turn_state,
    set_cached_tool_result,
    settings_of,
    tool,
)

# 与旧版 ``_build_tools()`` 返回顺序一致（模型看到的工具顺序不变）
TOOL_ORDER = (
    "ocr_img",
    "send_message",
    "get_message",
    "add_Method",
    "delete_Method",
    "alter_Method",
    "silent_observe",
    "poke",
    "emoji_like",
    "set_ban",
    "recall_msg",
    "send_dress",
    "get_msg_history",
    "update_person",
    "update_group",
)

_BUILDERS = (
    build_media_tools,
    build_chat_tools,
    build_history_tools,
    build_method_tools,
    build_moderation_tools,
    build_relation_tools,
)


def build_tools(
    agent: Any,
    *,
    groups: tuple[str, ...] | list[str] | None = None,
) -> list:
    """构建全部工具（或只构建 ``groups`` 指定的几组）。

    ``groups`` 接受 ``media`` / ``chat`` / ``history`` / ``methods`` / ``moderation``。
    """
    builders = _BUILDERS
    if groups is not None:
        wanted = {str(name) for name in groups}
        builders = tuple(
            builder
            for builder in _BUILDERS
            if builder.__name__.removeprefix("build_").removesuffix("_tools") in wanted
        )

    tools: list = []
    for builder in builders:
        tools.extend(builder(agent))

    order = {name: index for index, name in enumerate(TOOL_ORDER)}
    tools.sort(key=lambda item: order.get(getattr(item, "name", ""), len(order)))
    return tools


__all__ = [
    "TOOL_ORDER",
    "_MISSING",
    "_current_context_var",
    "_current_images_var",
    "_current_tool_cache_var",
    "_current_tool_calls_var",
    "_current_user_text_var",
    "api_of",
    "build_chat_tools",
    "build_history_tools",
    "build_media_tools",
    "build_method_tools",
    "build_moderation_tools",
    "build_relation_tools",
    "build_tools",
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
