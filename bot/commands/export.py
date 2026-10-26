"""``export``：把一段历史消息批量搬运到指定群 / 用户。

旧 ``Commands.CommandHandlers.export`` 的行为原样搬过来：

- 不带 ``-b / -e``：回复=起点，命令消息=终点，直接搬运（旧语义）；
- 带 ``-b / -e``：交给 :class:`RangeTaskService` 按群收集，两端 + 目标齐了再搬；
- 目标用 ``-g/-group``（群）或 ``-u/-user``（私聊）。

所谓「导出」就是让 ForwardService 按区间把消息转发出去；旧实现不写本地文件，
这里保持同样的输出方式（没有文件命名可保留）。
"""

from __future__ import annotations

import re

__all__ = [
    "export",
    "run_export_range",
    "run_export_forward",
    "range_ready",
    "parse_range",
]


def parse_range(clean_msg, reply_id, message_id):
    """解析 -begin/-b、-end/-e，返回 (begin_id, end_id)，没出现的一端为 None。

    同一条消息同时带 -b/-e 时：起点取回复目标，终点取命令消息本身。
    """
    has_begin = bool(re.search(r"(-begin|-b)", clean_msg))
    has_end = bool(re.search(r"(-end|-e)", clean_msg))
    begin_id = (reply_id or message_id) if has_begin else None
    end_id = None
    if has_end:
        end_id = message_id if has_begin else (reply_id or message_id)
    return begin_id, end_id


# 旧名字，保留给直接 import 的调用方 / 测试
_parse_range = parse_range


def range_ready(data) -> bool:
    """export 区间的完成条件：两端补齐，且至少有一个搬运目标。"""
    return data.get("target_group") is not None or data.get("target_user") is not None


# 旧名字
_export_range_ready = range_ready


async def export(handler, event) -> bool:
    """解析 ``export`` 命令：没有区间标记就直接搬，有标记就登记区间任务。"""
    DEBUG = True
    clean_msg = handler.mp.get_clean_context(event)
    rpy = handler.mp.is_reply(event)
    match1 = re.search(r"(-group|-g) (\d+)", clean_msg)
    match2 = re.search(r"(-user|-u) (\d+)", clean_msg)

    gid = str(event["group_id"])
    begin_id, end_id = parse_range(clean_msg, rpy, event["message_id"])
    has_target = bool(match1 or match2)
    has_marker = begin_id is not None or end_id is not None

    # 没有 -b/-e 标记：保持原语义，回复=起点，命令消息=终点，直接搬运
    if not has_marker:
        if not has_target:
            DEBUG and print("Find no Target")
            return False
        start_id = rpy
        end_id = event["message_id"]
        if not start_id:
            DEBUG and print("Find no Start")
            return False
        DEBUG and print(f"export range: {start_id} -> {end_id}")
        await run_export_forward(
            handler,
            event,
            start_id,
            end_id,
            target_group=int(match1.group(2)) if match1 else None,
            target_user=int(match2.group(2)) if match2 else None,
        )
        return True

    # 带标记：交给统一的区间服务收集 -b/-e（一条消息一段），
    # 目标可以出现在任一条消息里（或沿用之前存的）。
    data = {}
    if match1:
        data["target_group"] = int(match1.group(2))
    if match2:
        data["target_user"] = int(match2.group(2))
    await handler.ranges.submit(
        "export",
        gid,
        event=event,
        begin_id=begin_id,
        end_id=end_id,
        data=data,
        ready_when=range_ready,
    )
    return True


async def run_export_range(handler, event, data) -> None:
    """export 区间收齐后的回调：交给 auto_forward 搬运。"""
    DEBUG = True
    DEBUG and print(f"export range: {data['begin_id']} -> {data['end_id']}")
    await run_export_forward(
        handler,
        event,
        data["begin_id"],
        data["end_id"],
        target_group=data.get("target_group"),
        target_user=data.get("target_user"),
    )


async def run_export_forward(
    handler, event, start_id, end_id, target_group=None, target_user=None
) -> None:
    """把 [start_id, end_id] 区间交给 auto_forward 搬运到目标群 / 用户。"""
    if target_group is not None:
        await handler.mp.get_long_history_only_processor(
            group_id=event["group_id"],
            start_id=start_id,
            end_id=end_id,
            processor=handler.forward.auto_forward,
            params={
                "groupid_list": [event["group_id"]],
                "target_groupid": [target_group],
            },
        )
    if target_user is not None:
        print("Prepare to start export user")
        await handler.mp.get_long_history_only_processor(
            group_id=event["group_id"],
            start_id=start_id,
            end_id=end_id,
            processor=handler.forward.auto_forward,
            params={
                "groupid_list": [event["group_id"]],
                "task_userid": [target_user],
            },
        )
