"""``export``：把一段历史消息批量搬运到指定群 / 用户。

旧 ``Commands.CommandHandlers.export`` 的行为原样搬过来：

- 不带 ``-b / -e``：回复=起点，命令消息=终点，直接搬运（旧语义）；
- 带 ``-b / -e``：交给 :class:`RangeTaskService` 按群收集，两端 + 目标齐了再搬；
- 目标用 ``-g/-group``（群，可重复）/ ``-u/-user``（私聊）/ ``-a/-auto``（默认目标群）。

``-a`` 与 ``img_fd -a`` 同义：不写死群号，改用配置里的默认目标群
（``export.auto_target_group_ids``，留空则退回 ``auto_forward.target_groupid``）。
写了 ``-g`` 就以 ``-g`` 为准，``-a`` 只是「没写 -g 时的默认值」。

所谓「导出」就是让 ForwardService 按区间把消息转发出去；旧实现不写本地文件，
这里保持同样的输出方式（没有文件命名可保留）。
"""

from __future__ import annotations

import re

from core.logging import get_logger

__all__ = [
    "export",
    "run_export_range",
    "run_export_forward",
    "range_ready",
    "parse_range",
    "auto_targets",
]

# 目标参数：-g / -group 可重复出现，-u / -user 同理
_GROUP_RE = r"(?:-group|-g)\s+(\d+)"
_USER_RE = r"(?:-user|-u)\s+(\d+)"
_AUTO_RE = r"(?:^|\s)(?:-a|-auto)(?=\s|$)"

log = get_logger("bot.commands.export")


def _unique_ints(values) -> list:
    """去重并保持出现顺序（群号既可能来自 -g，也可能来自 -a）。"""
    seen, out = set(), []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        out.append(number)
    return out


def auto_targets(handler) -> list:
    """``export -a`` 时使用的默认目标群列表（来自配置）。

    优先 ``export.auto_target_group_ids``；这一项留空则退回
    ``auto_forward.target_groupid``（面板「自动搬运」里那组目标群），
    这样 ``-a`` 的默认行为与自动搬运保持一致，不用重复配一遍。
    """
    settings = getattr(handler, "settings", None)
    if settings is None:
        return []
    value = getattr(settings, "export_auto_targets", None)
    if value is not None:
        return _unique_ints(value)
    if isinstance(settings, dict):
        section = settings.get("export") or {}
        value = section.get("auto_target_group_ids") or []
        if not value:
            value = (settings.get("auto_forward") or {}).get("target_groupid") or []
        return _unique_ints(value)
    getter = getattr(settings, "get", None)
    if callable(getter):
        value = getter("export.auto_target_group_ids", []) or []
        if not value:
            value = getter("auto_forward.target_groupid", []) or []
        return _unique_ints(value)
    return []


def parse_targets(handler, clean_msg) -> tuple:
    """解析目标参数，返回 ``(群列表, 私聊 QQ)``。

    - ``-a`` 只在没写 ``-g`` 时生效（写了 ``-g`` 就按 ``-g`` 走）；
    - ``-u`` 与群目标互相独立，可以同时出现。
    """
    groups = [int(m) for m in re.findall(_GROUP_RE, clean_msg)]
    users = [int(m) for m in re.findall(_USER_RE, clean_msg)]
    has_auto = bool(re.search(_AUTO_RE, clean_msg))
    if not groups and has_auto:
        groups = auto_targets(handler)
    return _unique_ints(groups), (users[0] if users else None)


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
    return bool(
        data.get("target_groupid_list")
        or data.get("target_group") is not None
        or data.get("target_user") is not None
    )


# 旧名字
_export_range_ready = range_ready


async def export(handler, event) -> bool:
    """解析 ``export`` 命令：没有区间标记就直接搬，有标记就登记区间任务。"""
    clean_msg = handler.mp.get_clean_context(event)
    rpy = handler.mp.is_reply(event)

    gid = str(event["group_id"])
    begin_id, end_id = parse_range(clean_msg, rpy, event["message_id"])
    target_groups, target_user = parse_targets(handler, clean_msg)
    has_target = bool(target_groups) or target_user is not None
    has_marker = begin_id is not None or end_id is not None

    # 写了 -a 却一个默认目标群都没配：说清楚，别静默什么都不做
    if not has_target and re.search(_AUTO_RE, clean_msg):
        log.warning(
            "[export] -a 没有可用的默认目标群（export.auto_target_group_ids / "
            "auto_forward.target_groupid 都是空的）"
        )
        return False

    # 没有 -b/-e 标记：保持原语义，回复=起点，命令消息=终点，直接搬运
    if not has_marker:
        if not has_target:
            log.debug("find no target")
            return False
        start_id = rpy
        end_id = event["message_id"]
        if not start_id:
            log.debug("find no start")
            return False
        log.debug("export range: %s -> %s", start_id, end_id)
        await run_export_forward(
            handler,
            event,
            start_id,
            end_id,
            target_groups=target_groups,
            target_user=target_user,
        )
        return True

    # 带标记：交给统一的区间服务收集 -b/-e（一条消息一段），
    # 目标可以出现在任一条消息里（或沿用之前存的）。
    data = {}
    if target_groups:
        data["target_groupid_list"] = target_groups
    if target_user is not None:
        data["target_user"] = target_user
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
    log.debug("export range: %s -> %s", data["begin_id"], data["end_id"])
    await run_export_forward(
        handler,
        event,
        data["begin_id"],
        data["end_id"],
        target_groups=data.get("target_groupid_list"),
        target_user=data.get("target_user"),
        target_group=data.get("target_group"),
    )


async def run_export_forward(
    handler,
    event,
    start_id,
    end_id,
    target_group=None,
    target_user=None,
    target_groups=None,
) -> None:
    """把 [start_id, end_id] 区间交给 auto_forward 搬运到目标群 / 用户。

    :param target_group: 单个目标群（旧参数，保留兼容）
    :param target_groups: 目标群列表（``-g`` 可重复，或 ``-a`` 展开后的列表）
    :param target_user: 私聊目标 QQ
    """
    groups = []
    for value in (target_group, target_groups):
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            groups.extend(value)
        else:
            groups.append(value)
    groups = _unique_ints(groups)

    if groups:
        await handler.mp.get_long_history_only_processor(
            group_id=event["group_id"],
            start_id=start_id,
            end_id=end_id,
            processor=handler.forward.auto_forward,
            params={
                "groupid_list": [event["group_id"]],
                "target_groupid": groups,
            },
        )
    if target_user is not None:
        log.info("开始导出历史消息到用户 %s", target_user)
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
