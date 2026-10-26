"""历史相关工具：``get_msg_history`` / ``get_message``。

参数、返回文本、缓存语义与旧版一致。
``get_msg_history`` 优先用旧签名（``message_seq`` / ``count`` / ``reverse_order``），
新 MessageProcessor 若不接受关键字参数，则退化成把参数塞进 event。
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import (
    _MISSING,
    api_of,
    get_cached_tool_result,
    messaging_of,
    record_tool_call,
    set_cached_tool_result,
    tool,
)

logger = logging.getLogger("QQBotAgent.Tools.History")


async def _history(messaging: Any, group_id: int, anchor: int, count: int) -> Any:
    event = {"group_id": group_id, "message_type": "group"}
    try:
        # 同时传两个游标：NapCat 用 message_seq，SnowLuma 用 message_id。
        return await messaging.get_history_msg(
            event=event,
            message_seq=anchor,
            message_id=anchor,
            count=count,
            reverse_order=True,
        )
    except TypeError:
        # 新接口可能只接受一个 event；参数放到 event 里，语义不变。
        merged = {
            **event,
            "message_seq": anchor,
            "message_id": anchor,
            "count": count,
            "reverse_order": True,
        }
        return await messaging.get_history_msg(merged)


def build_history_tools(agent: Any) -> list:
    @tool
    async def get_msg_history(
        group_id: int, message_id: int, count: int = 20
    ) -> str:
        """获取指定群聊里，某条消息的之前count条聊天记录，用于了解对话上下文。
        参数:
          - group_id: int, 目标群号
          - message_id: int, 目标消息的 message_seq/message_id（用于获取该消息之前的聊天记录[包括该消息]）
          - count: int, 获取多少条历史消息（默认20条），最多51条如果需要更多，可以多次调用"""
        logger.info(
            f"[工具调用] get_msg_history(group_id={group_id}, message_id={message_id}, count={count})"
        )
        messaging = messaging_of(agent)
        if messaging is None:
            logger.warning("[工具] MessageProcessor 未连接")
            record_tool_call(
                "get_msg_history",
                {"group_id": group_id, "message_id": message_id, "count": count},
                error="MessageProcessor 未连接",
            )
            return "[工具不可用] MessageProcessor 未连接"
        res = await _history(messaging, group_id, message_id, count)
        print(res)
        return "获得历史上下文:" + str(res)

    @tool
    async def get_message(message_id: int) -> str:
        """根据消息 ID 获取消息的详细内容（用于查看被回复的那条消息说了什么）。
        参数:
          - message_id: int, 消息 ID"""
        logger.info(f"[工具调用] get_message(message_id={message_id})")
        napcat = api_of(agent)
        if napcat is None:
            logger.warning("[工具] NapCat API 未连接")
            record_tool_call(
                "get_message", {"message_id": message_id}, error="NapCat API 未连接"
            )
            return "[工具不可用] NapCat API 未连接"
        cached = get_cached_tool_result("get_message", {"message_id": message_id})
        if cached is not _MISSING:
            logger.info(f"[工具缓存] 复用 get_message 结果: {message_id}")
            record_tool_call("get_message", {"message_id": message_id}, result=cached)
            return cached
        try:
            resp = await napcat.get_message(message_id)
            data = resp.get("data", {})
            sender = data.get("sender", {}).get("nickname", "未知")
            raw = data.get("raw_message", "")
            logger.info(
                f"[工具结果] 消息 {message_id} | 发送者: {sender} | 内容: {raw[:60]}{'...' if len(raw)>60 else ''}"
            )
            result_text = f"消息ID {message_id} | 发送者: {sender} | 内容: {raw}"
            set_cached_tool_result(
                "get_message", {"message_id": message_id}, result_text
            )
            record_tool_call(
                "get_message",
                {"message_id": message_id},
                result={"sender": sender, "raw": raw},
            )
            return result_text
        except Exception as e:
            logger.error(f"[工具异常] 获取消息失败: {e}")
            record_tool_call(
                "get_message", {"message_id": message_id}, error=str(e)
            )
            return f"获取消息失败: {e}"

    return [get_msg_history, get_message]


__all__ = ["build_history_tools"]
