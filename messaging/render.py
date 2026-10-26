"""ChatContext 的文本渲染。

实现集中在 ``models.py``，这里保持一个稳定的导入路径：

    from messaging.render import render_message, render_body
"""

from .models import (
    MAX_CURRENT_CHARS,
    MAX_FORWARD_CHARS,
    MAX_FORWARD_ITEM_CHARS,
    MAX_FORWARD_MESSAGES,
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    MAX_QUOTE_CHARS,
    ChatContext,
    clip,
    is_legacy_context_note,
    message_summary,
    render_body,
    render_forward,
    render_message,
)

__all__ = [
    "MAX_CURRENT_CHARS",
    "MAX_FORWARD_CHARS",
    "MAX_FORWARD_ITEM_CHARS",
    "MAX_FORWARD_MESSAGES",
    "MAX_HISTORY_CHARS",
    "MAX_HISTORY_MESSAGES",
    "MAX_MESSAGE_CHARS",
    "MAX_NAME_CHARS",
    "MAX_QUOTE_CHARS",
    "ChatContext",
    "clip",
    "is_legacy_context_note",
    "message_summary",
    "render_body",
    "render_forward",
    "render_message",
]
