# -*- coding: utf-8 -*-
"""步骤一：这条事件要不要搬、按哪条路搬、搬给谁。

只做判断，不发送、不写库，返回 :class:`ForwardJob` 或 ``None``（None = 不归我管）。
判断顺序是有讲究的，别随便挪：

1. **机器人白名单先于来源白名单**。私聊里由白名单机器人发来的图片，``user_id``
   通常不在 ``userid_list`` 里；先卡来源就会把这类图片整条丢掉（``bot_list`` 里
   ``type=private`` 会变成死配置）。
2. **机器人自己的消息直接不搬**。目标群几乎都在来源群里，搬进去那张卡片会以
   ``uid=自己`` 回流；指纹对得上只是白跑一趟，对不上（weak 取不到图）就会二次搬运。
3. **类型判定最后做**。``old_forward`` / ``multimsg`` / 哔哩哔哩卡片都是聊天记录，
   共用一套指纹；只有白名单机器人的图片走独立送达库。
"""

from __future__ import annotations

from core.logging import get_logger
from messaging.forward_filter import is_sender_filtered

from ..dedup import clean_group_ids
from .job import KIND_IMAGE, KIND_MINIAPP, KIND_RECORD, ForwardJob

__all__ = ["detect_job"]

log = get_logger("bot.services.forward.detect")


def _without_self(values, self_value) -> list:
    """去掉「消息自己所在的群 / 人」，其余原样保留顺序。"""
    return [item for item in (values or []) if str(item) != str(self_value)]


def detect_job(
    service,
    event,
    *,
    is_recover=False,
    groupid_list=None,
    userid_list=None,
    target_groupid=None,
    task_userid=None,
    dedup=False,
    auto_img_forward_from_bot=None,
    strip_tail_ads=None,
    forward_filter=None,
) -> ForwardJob | None:
    """识别一条事件：

    :return: :class:`ForwardJob`（要处理）或 ``None``（不归我管）。
             ``job.blocked`` 非空表示「已处理但不搬」（命中了来源过滤器）。
    """
    event = event or {}
    message_type = event.get("message_type")
    if message_type not in ("group", "private"):
        return None

    bot_config = auto_img_forward_from_bot or {}
    # 机器人图片的范围判定必须先做：私聊来源闸门用得到它
    bot_image = (
        bool(bot_config.get("enable", False))
        and service._is_whitelisted_bot(event, bot_config)
        and service.mp.is_contain_image(event)
    )

    if message_type == "group":
        if str(event.get("group_id")) not in clean_group_ids(groupid_list):
            return None
    elif str(event.get("user_id")) not in clean_group_ids(userid_list) and not bot_image:
        return None

    if service._is_self_event(event):
        # 只给「卡片类」留一行日志：机器人自己说的话比卡片多得多，全打会淹掉控制台
        if service._check_is_old_forward(event) or service._check_is_multimsg_forward(event):
            log.info(
                "忽略机器人自己发出的消息（group=%s，message_id=%s）",
                event.get("group_id"),
                event.get("message_id"),
            )
        return None

    self_group = str(event.get("group_id") or "")
    job = ForwardJob(
        event=event,
        is_recover=bool(is_recover),
        self_group_is_target=bool(self_group)
        and any(str(g) == self_group for g in (target_groupid or [])),
        target_groups=_without_self(target_groupid, event.get("group_id")),
        target_users=_without_self(task_userid, event.get("user_id")),
        dedup=bool(dedup),
        strip_tail_ads=strip_tail_ads,
        bot_config=bot_config,
    )

    if bot_image:
        job.kind = KIND_IMAGE
        return job

    miniapp_info = service._check_is_miniapp(event) or {}
    job.miniapp_info = miniapp_info
    is_chat_record = bool(
        service._check_is_old_forward(event) or service._check_is_multimsg_forward(event)
    )
    is_miniapp = miniapp_info.get("title") == "哔哩哔哩"
    if not (is_chat_record or is_miniapp):
        return None

    if is_chat_record and is_sender_filtered(event, forward_filter):
        log.info(
            "来源群 %s 的发送者 %s 在过滤名单里，不搬运这条聊天记录",
            event.get("group_id"),
            event.get("user_id"),
        )
        job.blocked = "sender_filtered"
        return job

    job.kind = KIND_MINIAPP if is_miniapp else KIND_RECORD
    return job
