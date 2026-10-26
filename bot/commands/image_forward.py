"""``img_fd``：创建图片批量搬运任务（``-g`` / ``-u`` / ``-a`` + ``-b`` / ``-e``）。

- ``-g/-group <群号>``：搬到指定群；
- ``-u/-user <QQ>``：搬到指定私聊（旧版接口保留，实际发送只实现了群）；
- ``-a/-auto``：用 ``settings.image_forward.auto_target_group_ids``（面板可改）；
- 区间仍由 :class:`RangeTaskService` 按群收集，收齐后调
  ``forward.image_forward_batch`` 合并转发。

旧代码里 ``-a`` 走的是 ``BotConfig.image_forward_auto_targets``；新配置对象上是同名
属性 ``BotSettings.image_forward_auto_targets``。
"""

from __future__ import annotations

import re

from .export import parse_range

__all__ = ["create_task", "run_img_range", "auto_targets", "range_ready"]


def auto_targets(handler) -> list:
    """``img_fd -a`` 时使用的默认目标群列表（来自配置）。"""
    settings = getattr(handler, "settings", None)
    value = getattr(settings, "image_forward_auto_targets", None)
    if value is not None:
        return list(value or [])
    if isinstance(settings, dict):
        section = settings.get("image_forward") or {}
        return list(section.get("auto_target_group_ids") or [])
    return []


def range_ready(data) -> bool:
    """img_fd 区间的完成条件：两端补齐，且登记过搬运目标。"""
    return data.get("target_id_list") is not None and data.get("msg_type") is not None


# 旧名字
_img_range_ready = range_ready


async def create_task(handler, event):
    clean_msg = handler.mp.get_clean_context(event=event)
    rpy = handler.mp.is_reply(event)
    data = {}
    match = re.search(r"(-g|-group) (\d+)", clean_msg)
    if match:
        data = {"target_id_list": [int(match.group(2))], "msg_type": "group"}
    else:
        match = re.search(r"(-u|-user) (\d+)", clean_msg)
        if match:
            data = {"target_id_list": [int(match.group(2))], "msg_type": "private"}
        else:
            match = re.search(r"(-a|-auto)", clean_msg)
            if match:
                data = {
                    "target_id_list": auto_targets(handler),
                    "msg_type": "group",
                }

    begin_id, end_id = parse_range(clean_msg, rpy, event["message_id"])
    await handler.ranges.submit(
        "img_forward_create_task",
        event["group_id"],
        event=event,
        begin_id=begin_id,
        end_id=end_id,
        data=data,
        ready_when=range_ready,
    )
    return


async def run_img_range(handler, event, data) -> None:
    """img_fd 区间收齐后的回调：合并转发区间内的图片。"""
    await handler.forward.image_forward_batch(
        group_id=event["group_id"],
        target_id_list=data["target_id_list"],
        begin_id=data["begin_id"],
        end_id=data["end_id"],
        msg_type=data["msg_type"],
    )
