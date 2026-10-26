"""对话类工具：send_message / send_dress / silent_observe。"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from ..hints import _dress_is_just_talk
from ..output import render_message_body
from .registry import (
    _MISSING,
    _current_user_text_var,
    get_cached_tool_result,
    messaging_of,
    record_tool_call,
    set_cached_tool_result,
    settings_of,
    tool,
)

logger = logging.getLogger("QQBotAgent.Tools.Chat")

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _dress_dir(agent: Any) -> Path:
    """女装照目录：优先 ImageResolver.images_dir，其次 settings.paths.images_dir。"""
    images = getattr(agent, "images", None)
    if images is not None:
        try:
            return Path(images.images_dir) / "dress"
        except Exception:
            pass
    settings = settings_of(agent)
    if settings is not None:
        paths = getattr(settings, "paths", None)
        if paths is not None and getattr(paths, "images_dir", None) is not None:
            return Path(paths.images_dir) / "dress"
        imgs = str(getattr(settings, "imgs_dir", "") or "").strip()
        if imgs:
            return Path(imgs) / "dress"
    return Path(__file__).resolve().parents[2] / "imgs" / "dress"


def _dress_image_addr(agent: Any, path: Path) -> str:
    """把选中的照片转成 ``send_img`` 能接受的地址（NapCat 视角）。"""
    images = getattr(agent, "images", None)
    if images is not None:
        try:
            ref = images.dress(path.name)
            addr = getattr(ref, "napcat_path", None)
            if addr:
                return addr
        except Exception:
            pass
        try:
            return images.to_napcat(path)
        except Exception:
            pass
    settings = settings_of(agent)
    if settings is not None:
        base = str(getattr(settings, "imgs_dir", "") or "").strip()
        if base:
            return f"{base}/dress/{path.name}"
    return str(path).replace("\\", "/")


def build_chat_tools(agent: Any) -> list:
    @tool(return_direct=True)
    async def send_message(message_type: str, group_id: int, user_id: int, message: str) -> str:
        """把消息发送到指定群 / 私聊 / 转发时使用（默认回复请直接写在 <output><message> 里，不要调用本工具）。
        仅当用户明确要求把消息发到别的群、转发给别人、或发起私聊时才调用；一次调用即可，发送后不要再重复输出相同内容。
        参数:
          - message_type: str, 消息类型（"group" 或 "private"）
          - group_id: int, 目标群号
          - user_id: int, 目标用户 QQ 号（仅在 private 消息时使用）
          - message: str, 要发送的消息内容（可含 <meme>表情名</meme>）"""
        logger.info(
            f"[工具调用] send_message(group_id={group_id}, message_type={message_type}, user_id={user_id}, message={message[:60]}{'...' if len(message)>60 else ''})"
        )
        args = {
            "message_type": message_type,
            "group_id": group_id,
            "user_id": user_id,
            "message": message,
        }
        cached = get_cached_tool_result("send_message", args)
        if cached is not _MISSING:
            logger.info(f"[工具缓存] 复用 send_message 结果: {message[:40]}...")
            record_tool_call("send_message", args, result=cached)
            return cached
        messaging = messaging_of(agent)
        if messaging is None:
            logger.warning("[工具] MessageProcessor 未连接")
            record_tool_call("send_message", args, error="MessageProcessor 未连接")
            return "[工具不可用] MessageProcessor 未连接"
        try:
            await messaging.send_message(
                event={
                    "message_type": message_type,
                    "group_id": group_id,
                    "user_id": user_id,
                },
                # 工具收到的是一条「原始输出」：可能带 <meme> / <at>，
                # 先渲染成 CQ 码，否则标签会被原样发出去
                message=render_message_body(message),
            )
            logger.info(f"[工具结果] 群消息已发送到 {group_id}")
            result_text = f"已成功发送群消息到群 {group_id}"
            set_cached_tool_result("send_message", args, result_text)
            record_tool_call("send_message", args, result="成功")
            return result_text
        except Exception as e:
            logger.error(f"[工具异常] 发送群消息失败: {e}")
            record_tool_call("send_message", args, error=str(e))
            return f"发送群消息失败: {e}"

    @tool
    async def send_dress(group_id=None, message_type="group", user_id=None) -> str:
        """发送女装照(大部分都是腿照)
        参数:
          - group_id: int, 目标群号//如果要发送私聊则传入user_id
          - message_type: str, 消息类型 'group' 或 'private'
          - user_id: int, 如果私聊,要发送的用户ID
        """
        logger.info(
            f"[工具调用] send_dress(group_id={group_id}, message_type={message_type}, user_id={user_id})"
        )
        args = {"group_id": group_id, "message_type": message_type, "user_id": user_id}
        # 对方只是在聊女装（「你为什么羡慕女装」这类提问）→ 不要发图，改成文字回答
        turn_text = _current_user_text_var.get("")
        # 注意：这里**不记**带副作用的成功记录 —— 一旦记成 send_dress，
        # chat() 会把本轮当成「已经发过东西」，于是该回的话也不再补。
        if _dress_is_just_talk(turn_text):
            logger.info(
                "[工具] send_dress 已跳过：对方只是在聊女装，没有要照片"
            )
            return (
                "对方只是在聊女装 / 问你问题，并没有要照片。"
                "不要发图，也不要提照片的事，直接用 <message> 文字回答他问的问题。"
            )
        dress_dir = _dress_dir(agent)
        # 只挑图片，别把 README 之类的文件当成照片发出去
        try:
            files = [
                f
                for f in dress_dir.iterdir()
                if f.is_file() and f.suffix.lower() in _IMAGE_SUFFIXES
            ]
        except OSError:
            files = []
        if not files:
            logger.warning(f"[工具] send_dress 失败 - {dress_dir} 下没有图片")
            record_tool_call(args=args, tool_name="send_dress", error="没有可用的女装照")
            return "未找到女装照"
        chosen = random.choice(files)
        img_addr = _dress_image_addr(agent, chosen)
        messaging = messaging_of(agent)
        try:
            await messaging.send_img(
                event={
                    "group_id": group_id,
                    "message_type": message_type,
                    "user_id": user_id,
                },
                img_addr=img_addr,
                summary="女装照",
            )
        except Exception as e:
            logger.error(f"[工具异常] send_dress 失败: {e}")
            record_tool_call("send_dress", args, error=str(e))
            return f"发送女装照失败: {e}"
        logger.info(f"[工具结果] send_dress -> {img_addr}")
        record_tool_call(
            "send_dress",
            args,
            result="已发送女装照（照片就发出去了，别再重复发）",
        )
        return "已发送女装照"

    @tool
    async def silent_observe(reason: str) -> str:
        """当你觉得当前消息不需要回复时（例如话题无聊、对方自言自语、你不想说话、静默观察等），
        调用此工具表示沉默观察，调用后你不会发送任何消息到群里或私聊。
        参数:
          - reason: str, 沉默的原因（仅用于记录日志）。"""
        logger.info(f"[工具调用] silent_observe(reason='{reason[:60]}')")
        record_tool_call("silent_observe", {"reason": reason}, result="已静默")
        return "__SILENT__"

    return [send_message, send_dress, silent_observe]


__all__ = ["build_chat_tools"]
