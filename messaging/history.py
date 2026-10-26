"""历史消息拉取与长区间翻页。

旧代码在 ``NapCatTools.py`` 里，这里按职责单独放一份。所有函数第一个参数是
:class:`messaging.processor.MessageProcessor`（``mp``），方法只是薄封装。

**游标因后端而异**：NapCat 的 ``get_group_msg_history`` 认 ``message_seq``，
SnowLuma 认 ``message_id``（有符号 int32，可能为负）。这里统一用
:mod:`core.anchors`：每条消息的 ``message_seq`` / ``message_id`` 都交给
``mp.nc``，由 ``transport.api`` 按后端挑一个发出去；比较区间终点时同时比对
所有游标，避免 SnowLuma 下 ``message_seq`` 对不上。

普通 ``get_history_msg`` / ``collect_history`` 也从当前事件取两个游标；
引用消息仍然用 ``message_id``（``get_msg``）。
"""

from __future__ import annotations

from datetime import datetime

from core.anchors import anchor_kwargs, anchor_matches, anchor_of

from .models import render_message


async def get_group_latest_msg(mp, group_id):
    """取群里最新一条原始事件（断点用）。"""
    await mp.ensure_connected()
    event = await mp.nc.get_group_msg_history(
        group_id=group_id, count=1, reverse_order=True
    )
    messages = event["data"]["messages"]
    if not messages:
        print(f"[警告] 群 {group_id} 暂无消息或无法访问")
        return None
    return messages[0]


async def get_long_history_backward(mp, group_id, start_id, end_id):
    """从 ``start_id`` 往后翻到 ``end_id``（游标由后端决定）。"""
    DEBUG = False
    remaining = 20  # 最大搜索 20 次
    part = []
    DEBUG = True
    cursor = anchor_kwargs(message_seq=start_id, message_id=start_id)
    while remaining > 0:
        DEBUG and print("reamaining = ", remaining)
        await mp.ensure_connected()
        icl_history = await mp.nc.get_group_msg_history(
            group_id=group_id, **cursor
        )
        messages = icl_history["data"]["messages"]
        if not messages:
            # 旧实现这里会 IndexError / NameError；请求失败时直接返回已拿到的部分
            break
        last = messages[-1]
        cursor = anchor_of(last).as_kwargs()
        messages.pop()
        part = messages + part
        remaining -= 1
        if any(anchor_matches(item, end_id) for item in messages):
            return part
    DEBUG and print("NotFind")
    return part


async def get_long_history_only_processor(mp, group_id, start_id, end_id, processor, params) -> None:
    """从 ``start_id`` 往后翻，逐条交给 ``processor(event=item, **params)``。

    区间终点同时比对 ``message_seq`` / ``message_id`` / ``real_id``：NapCat 用
    seq，SnowLuma 用 id，调用方不需要知道当前后端。
    """
    remaining = 200  # 最大搜索 200 次
    DEBUG = True

    cursor = anchor_kwargs(message_seq=start_id, message_id=start_id)

    while remaining > 0:
        print("reamaining = ", remaining)
        await mp.ensure_connected()
        icl_history = await mp.nc.get_group_msg_history(
            group_id=group_id, **cursor
        )
        messages = icl_history["data"]["messages"]
        if not messages:
            return
        last = messages[-1]
        cursor = anchor_of(last).as_kwargs()
        last_msg = messages.pop()
        remaining -= 1
        for item in messages:
            await processor(event=item, **params)
            if anchor_matches(item, end_id):
                DEBUG and print("FIND")
                return
        if anchor_matches(last_msg, end_id):
            await processor(event=last_msg, **params)
            DEBUG and print("FIND")
            return
    return


async def get_long_history(mp, group_id, message_id, remaining: int):
    """向前补一段历史（每次用第一条消息的游标继续往前翻）。

    旧行为：``message_id`` 参数实际被当成 ``message_seq`` 传给接口。现在两个
    游标都带上，由后端决定用哪个。
    """
    if remaining <= 0:
        return []
    part = []
    DEBUG = True
    cursor = anchor_kwargs(message_seq=message_id, message_id=message_id)
    while remaining > 0:
        DEBUG and print("reamaining = ", remaining)
        await mp.ensure_connected()
        icl_history = await mp.nc.get_group_msg_history(
            group_id=group_id, **cursor
        )
        messages = icl_history["data"]["messages"]
        if not messages:
            break
        first = messages[0]
        cursor = anchor_of(first).as_kwargs()
        DEBUG and print(messages)
        DEBUG and print("cursor", cursor)
        messages.pop(0)
        part = messages + part
        remaining -= 1

    return part


async def get_history_events(
    mp,
    event=None,
    message_seq=None,
    count: int = 6,
    reverse_order: bool = False,
    *,
    message_id=None,
) -> list[dict]:
    """拉原始历史事件（群聊/私聊自动分流）；失败返回空列表，不让上层崩。

    ``message_seq`` / ``message_id`` 只传一个时，把它当作通用游标（两个都带）；
    都不传时从 ``event`` 里取。真正发哪个字段由 ``mp.nc`` 的后端决定。
    """
    if not event:
        return []
    anchors = anchor_kwargs(
        event,
        message_seq=message_seq,
        message_id=message_id,
    )
    try:
        await mp.ensure_connected()
        if event.get("message_type") == "group":
            res = await mp.nc.get_group_msg_history(
                group_id=event["group_id"],
                reverse_order=reverse_order,
                count=count,
                **anchors,
            )
        elif event.get("message_type") == "private":
            res = await mp.nc.get_friend_msg_history(
                user_id=event["user_id"],
                reverse_order=reverse_order,
                count=count,
                **anchors,
            )
        else:
            return []
    except Exception as e:
        print(f"[上下文] 拉历史消息失败: {e}")
        return []
    return (res.get("data") or {}).get("messages") or []


async def collect_history(
    mp,
    event=None,
    message_seq=None,
    count: int = 6,
    reverse_order: bool = False,
    *,
    message_id=None,
    master_ids=(),
    now: datetime | None = None,
    expand_quote: bool = True,
) -> list:
    """把历史事件批量补全成 ChatMessage。

    先把整批事件按 ``message_id`` 放进缓存，历史内部的互相引用就能本地解析，
    只有引用了更早的消息才会额外发包。
    """
    raws = await get_history_events(
        mp,
        event=event,
        message_seq=message_seq,
        message_id=message_id,
        count=count,
        reverse_order=reverse_order,
    )
    cache: dict = {}
    for raw in raws:
        for key in (raw.get("message_id"), raw.get("real_id")):
            if key not in (None, ""):
                cache[str(key)] = raw
    return [
        await mp.collect_message(
            raw,
            master_ids=master_ids,
            now=now,
            expand_quote=expand_quote,
            expand_forward=False,
            cache=cache,
        )
        for raw in raws
    ]


async def get_history_msg(
    mp,
    event=None,
    message_seq=None,
    count: int = 6,
    reverse_order: bool = False,
    *,
    message_id=None,
) -> str:
    """历史消息的结构化文本（工具与校园网助手用）。"""
    messages = await collect_history(
        mp,
        event=event,
        message_seq=message_seq,
        message_id=message_id,
        count=count,
        reverse_order=reverse_order,
    )
    return "\n".join(render_message(m, now=m.time) for m in messages)


async def get_long_history_test(mp, event):
    """调试入口：``get_long_history`` 的薄封装（旧行为：只打印，不返回）。"""
    DEBUG = False
    DEBUG and print("enter History Debug")
    res = await get_long_history(
        mp, group_id=event["group_id"], message_id=event["message_id"], remaining=10
    )
    DEBUG and print(res)
    DEBUG and print("end")


__all__ = [
    "collect_history",
    "get_group_latest_msg",
    "get_history_events",
    "get_history_msg",
    "get_long_history",
    "get_long_history_backward",
    "get_long_history_only_processor",
    "get_long_history_test",
]
