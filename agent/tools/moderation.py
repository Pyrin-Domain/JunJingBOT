"""系统动作工具：poke / emoji_like / set_ban / recall_msg。

全是「非语言动作」，参数、副作用、返回文本与旧版一致。
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import api_of, record_tool_call, tool

logger = logging.getLogger("QQBotAgent.Tools.Moderation")


def build_moderation_tools(agent: Any) -> list:
    @tool
    async def poke(user_id: int, group_id: int = 0) -> str:
        """戳一戳某个 QQ 用户（系统级非语言动作）。
        适用：想引起某人注意、对方无视你、俏皮地打招呼、想调侃对方。
        注意：不要用它替代回复；戳完仍然要按 <output> 格式给出文字（或空回复）。
        参数:
          - user_id: int, 要戳的 QQ 号
          - group_id: int, 在群里戳时填当前群号；私聊戳时填 0 或留空
        """
        logger.info(f"[工具调用] poke(user_id={user_id}, group_id={group_id})")
        napcat = api_of(agent)
        if napcat is None:
            logger.warning("[工具] NapCat API 未连接")
            record_tool_call(
                "poke", {"user_id": user_id, "group_id": group_id},
                error="NapCat API 未连接",
            )
            return "[工具不可用] NapCat API 未连接"
        try:
            await napcat.send_poke(user_id=user_id, group_id=group_id or None)
            logger.info(f"[工具结果] poke -> 已戳 {user_id}")
            record_tool_call(
                "poke", {"user_id": user_id, "group_id": group_id}, result="已戳一戳"
            )
            return f"已戳一戳 {user_id}"
        except Exception as e:
            logger.error(f"[工具异常] poke 失败: {e}")
            record_tool_call(
                "poke", {"user_id": user_id, "group_id": group_id}, error=str(e)
            )
            return f"戳一戳失败: {e}"

    @tool
    async def emoji_like(message_id: int, emoji_id: str = "66") -> str:
        """对某条消息做表情回应（系统级非语言动作）。
        参数:
          - message_id: int, 目标消息的消息 ID
          - emoji_id: str, 表情 ID，常用：424=赞同(红色按钮)、10068=问号、265=逆天(地铁老人手机)、76=赞、66=爱心
        """
        logger.info(
            f"[工具调用] emoji_like(message_id={message_id}, emoji_id={emoji_id})"
        )
        napcat = api_of(agent)
        if napcat is None:
            logger.warning("[工具] NapCat API 未连接")
            record_tool_call(
                "emoji_like", {"message_id": message_id, "emoji_id": emoji_id},
                error="NapCat API 未连接",
            )
            return "[工具不可用] NapCat API 未连接"
        try:
            await napcat.set_msg_emoji_like(
                message_id=message_id, emoji_id=str(emoji_id)
            )
            logger.info(f"[工具结果] emoji_like -> {message_id} 回应 {emoji_id}")
            record_tool_call(
                "emoji_like",
                {"message_id": message_id, "emoji_id": emoji_id},
                result=f"已回应该消息 {emoji_id}",
            )
            return f"已对消息 {message_id} 回应表情 {emoji_id}"
        except Exception as e:
            logger.error(f"[工具异常] emoji_like 失败: {e}")
            record_tool_call(
                "emoji_like", {"message_id": message_id, "emoji_id": emoji_id},
                error=str(e),
            )
            return f"表情回应失败: {e}"

    @tool
    async def set_ban(group_id: int, user_id: int, duration: int = 60) -> str:
        """禁言群成员（危机时刻才用，非必要绝不使用；开玩笑说“禁言我”时严禁调用）。
        参数:
          - group_id: int, 群号
          - user_id: int, 要禁言的 QQ 号
          - duration: int, 禁言时长（秒），0 表示解除禁言，最长 2592000
        """
        logger.info(
            f"[工具调用] set_ban(group_id={group_id}, user_id={user_id}, duration={duration})"
        )
        napcat = api_of(agent)
        if napcat is None:
            logger.warning("[工具] NapCat API 未连接")
            record_tool_call(
                "set_ban",
                {"group_id": group_id, "user_id": user_id, "duration": duration},
                error="NapCat API 未连接",
            )
            return "[工具不可用] NapCat API 未连接"
        try:
            await napcat.set_group_ban(
                group_id=group_id, user_id=user_id, duration=int(duration)
            )
            logger.info(
                f"[工具结果] set_ban -> {user_id} 禁言 {duration}s"
            )
            record_tool_call(
                "set_ban",
                {"group_id": group_id, "user_id": user_id, "duration": duration},
                result=f"已禁言 {user_id} {duration} 秒",
            )
            return f"已禁言 {user_id} {duration} 秒（0 表示解除）"
        except Exception as e:
            logger.error(f"[工具异常] set_ban 失败: {e}")
            record_tool_call(
                "set_ban",
                {"group_id": group_id, "user_id": user_id, "duration": duration},
                error=str(e),
            )
            return f"禁言失败: {e}"

    @tool
    async def recall_msg(message_id: int) -> str:
        """撤回指定消息（系统级非语言动作）。
        参数:
          - message_id: int, 要撤回的消息 ID
        """
        logger.info(f"[工具调用] recall_msg(message_id={message_id})")
        napcat = api_of(agent)
        if napcat is None:
            logger.warning("[工具] NapCat API 未连接")
            record_tool_call(
                "recall_msg", {"message_id": message_id}, error="NapCat API 未连接"
            )
            return "[工具不可用] NapCat API 未连接"
        try:
            await napcat.delete_msg(message_id=message_id)
            logger.info(f"[工具结果] recall_msg -> 已撤回 {message_id}")
            record_tool_call(
                "recall_msg", {"message_id": message_id}, result="已撤回"
            )
            return f"已撤回消息 {message_id}"
        except Exception as e:
            logger.error(f"[工具异常] recall_msg 失败: {e}")
            record_tool_call(
                "recall_msg", {"message_id": message_id}, error=str(e)
            )
            return f"撤回失败: {e}"

    return [poke, emoji_like, set_ban, recall_msg]


__all__ = ["build_moderation_tools"]
