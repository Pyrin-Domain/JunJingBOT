"""``recall``：批量撤回一段历史消息（``-b`` / ``-e`` 标出区间）。

行为与旧 ``Commands.CommandHandlers.recall`` 一致：

- 只支持群聊；
- 发起人必须是群主 / 管理员或 master；
- Bot 不是管理员时只能撤回自己的消息；
- 逐条撤回，每条之间 sleep 0.1 秒；「完成统计」的发送在旧代码里是注释掉的，
  这里同样不发。
"""

from __future__ import annotations

import asyncio
from functools import partial

from .export import parse_range

__all__ = ["recall", "run_recall_range", "do_recall", "recall_processor"]


def _master_qq(handler) -> list[str]:
    settings = getattr(handler, "settings", None)
    value = getattr(settings, "master_qq", None)
    if value is not None:
        if isinstance(value, (list, tuple, set)):
            return [str(qq) for qq in value]
        return [str(value)]
    if isinstance(settings, dict):
        raw = settings.get("master_qq") or []
        if isinstance(raw, (list, tuple, set)):
            return [str(qq) for qq in raw]
        return [str(raw)]
    return []


async def recall(handler, event) -> bool:
    """批量撤回：recall -b（回复起点）→ recall -e（回复终点），仅限本群聊。"""
    DEBUG = True
    if event.get("message_type") != "group":
        DEBUG and print("[批量撤回] 仅限群聊使用")
        return False
    clean_msg = handler.mp.get_clean_context(event)
    rpy = handler.mp.is_reply(event)
    begin_id, end_id = parse_range(clean_msg, rpy, event["message_id"])
    if begin_id is None and end_id is None:
        return False

    await handler.ranges.submit(
        "recall",
        str(event["group_id"]),
        event=event,
        begin_id=begin_id,
        end_id=end_id,
    )
    return True


async def run_recall_range(handler, event, data) -> None:
    """recall 区间收齐后的回调：权限检查 + 逐条撤回。"""
    await do_recall(handler, event, data["begin_id"], data["end_id"])


async def _get_member_role(handler, group_id, user_id) -> str:
    """取群成员角色（owner / admin / member），取不到返回空串。"""
    try:
        await handler.mp.ensure_connected()
        res = await handler.api.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
        data = res.get("data") or {}
        return str(data.get("role") or "")
    except Exception as exc:
        print(f"[批量撤回] 获取成员信息失败 user_id={user_id}: {exc!r}")
        return ""


async def do_recall(handler, event, start_id, end_id) -> None:
    """权限检查 + 逐条撤回 [start_id, end_id] 区间内的消息。"""
    DEBUG = True
    group_id = event["group_id"]
    # 发起人必须是群主/管理员或 master
    issuer_role = await _get_member_role(handler, group_id, event["user_id"])
    if issuer_role not in ("owner", "admin") and str(event["user_id"]) not in _master_qq(handler):
        await handler.mp.send_message(event, "[批量撤回] 需要管理员权限")
        return
    # bot 是否为管理员（非管理员只能撤自己的消息）
    bot_role = await _get_member_role(handler, group_id, handler.mp.user_id)
    is_bot_admin = bot_role in ("owner", "admin")
    # if int(start_id) > int(end_id):
    #     start_id, end_id = end_id, start_id
    stats = {"ok": 0, "fail": 0, "skip": 0}
    await handler.mp.get_long_history_only_processor(
        group_id=group_id,
        start_id=start_id,
        end_id=end_id,
        processor=partial(recall_processor, handler),
        params={
            "group_id": group_id,
            "is_bot_admin": is_bot_admin,
            "bot_user_id": str(handler.mp.user_id),
            "stats": stats,
        },
    )
    # await handler.mp.send_message(
    #     event,
    #     f"[批量撤回] 完成：成功 {stats['ok']}，失败 {stats['fail']}，跳过 {stats['skip']}",
    # )


async def recall_processor(
    handler, event=None, group_id=None, is_bot_admin=False,
    bot_user_id="", stats=None,
):
    """逐条撤回处理器（get_long_history_only_processor 回调）。

    旧实现是绑定方法；拆成模块后 handler 用 ``functools.partial`` 绑在最前面，
    回调签名仍是 ``processor(event=..., group_id=..., ...)``。
    """
    stats = stats if stats is not None else {"ok": 0, "fail": 0, "skip": 0}
    # 仅限本群聊：防串群
    msg_group = event.get("group_id")
    if msg_group is None or int(msg_group) != int(group_id):
        stats["skip"] += 1
        return
    # bot 非管理员时只能撤自己的消息
    if not is_bot_admin and str(event.get("user_id")) != bot_user_id:
        stats["skip"] += 1
        return
    try:
        await handler.mp.ensure_connected()
        await handler.api.delete_msg(event["message_id"])
        stats["ok"] += 1
    except Exception as exc:
        stats["fail"] += 1
        print(f"[批量撤回] 撤回失败 message_id={event.get('message_id')}: {exc!r}")
    await asyncio.sleep(0.1)
