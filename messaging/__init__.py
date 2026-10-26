"""消息数据层：结构化消息、渲染、历史、转发、上下文、收发工具。

这一层**不 import agent / bot**，也不直接 import transport；
需要发消息时接收一个 API 协议对象（见 :mod:`messaging.processor`）。

常用入口::

    from messaging import MessageProcessor, ChatContext, build_chat_context
    from messaging import get_miniapp_info, get_multimsg_resid
"""

from .attention import (
    ABOUT_ME,
    AMBIENT,
    SUMMON,
    Attention,
    detect_attention,
    detect_attention_of,
    message_text,
)
from .context_builder import build_chat_context, collect_message
from .forward import (
    check_is_miniapp,
    check_is_multimsg_forward,
    check_is_old_forward,
    get_miniapp_info,
    get_multimsg_detail,
    get_multimsg_resid,
)
from .forward_filter import (
    build_append_nodes,
    build_forward_nodes,
    build_nodes,
    normalize_settings as normalize_forward_filter,
    strip_tail_ads,
)
from .history import (
    collect_history,
    get_group_latest_msg,
    get_history_events,
    get_history_msg,
    get_long_history,
    get_long_history_backward,
    get_long_history_only_processor,
    get_long_history_test,
)
from .meme import MemeEntry, MemeLibrary, get_library, to_segments
from .models import (
    ChatContext,
    ChatMessage,
    Quote,
    Segment,
    Sender,
    parse_event,
    render_body,
    render_forward,
    render_message,
)
from .processor import MessageProcessor
from .thread_history import (
    ThreadHistory,
    ThreadHistoryStore,
    make_bot_message,
    merge_history,
)

__all__ = [
    # 注意力（这条消息在叫它 / 在说它 / 跟它无关）
    "ABOUT_ME",
    "AMBIENT",
    "SUMMON",
    "Attention",
    "detect_attention",
    "detect_attention_of",
    "message_text",
    # 数据模型 / 渲染
    "ChatContext",
    "ChatMessage",
    "Quote",
    "Segment",
    "Sender",
    "parse_event",
    "render_body",
    "render_forward",
    "render_message",
    # 表情包
    "MemeEntry",
    "MemeLibrary",
    "get_library",
    "to_segments",
    # 临时会话历史
    "ThreadHistory",
    "ThreadHistoryStore",
    "merge_history",
    "make_bot_message",
    # 处理器
    "MessageProcessor",
    # 上下文
    "build_chat_context",
    "collect_message",
    # 历史
    "collect_history",
    "get_group_latest_msg",
    "get_history_events",
    "get_history_msg",
    "get_long_history",
    "get_long_history_backward",
    "get_long_history_only_processor",
    "get_long_history_test",
    # 转发 / 卡片
    "check_is_miniapp",
    "check_is_multimsg_forward",
    "check_is_old_forward",
    "get_miniapp_info",
    "get_multimsg_detail",
    "get_multimsg_resid",
    # 转发末尾广告裁剪 + 尾部追加
    "build_append_nodes",
    "build_forward_nodes",
    "build_nodes",
    "normalize_forward_filter",
    "strip_tail_ads",
]
