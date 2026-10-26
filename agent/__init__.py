"""Agent 子系统：LLM 对话、工具、提示词、模型分级、校园网助手。

对外入口：

    from agent import QQBotAgent, CampusAssistant

提示词 / 输出协议 / 工具拆分在子模块里；业务层通常只需要 ``QQBotAgent``。
"""

from .campus import CampusAssistant
from .output import parse_agent_output, split_sendable_messages
from .runtime import LLMCallbackHandler, MODEL_TYPE, QQBotAgent

__all__ = [
    "QQBotAgent",
    "CampusAssistant",
    "LLMCallbackHandler",
    "MODEL_TYPE",
    "parse_agent_output",
    "split_sendable_messages",
]
