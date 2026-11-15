"""审计群「复搬」：对被过滤门拦下的内容做人工确认放行。

背景：``block_matched`` 开着时，命中过滤的内容只发审计群打标签，不搬给普通目标。
人工看了标签之后想真的搬——以前没路。现在：**回复那条【过滤标签】消息发「复搬」**，
Bot 从标签文本里解析出来源群 + 消息 id，登记放行后按自动搬运配置重新搬一遍。

设计要点：

* **无状态定位**：标签文本里本来就写着 ``来源群：`` 和 ``消息：``（见
  :func:`bot.services.forward.image_gate._format_tag_text`），直接解析，不用在内存里
  存「标签消息 → 原事件」的映射，重启也不丢；
* **定位信息写的是外层消息**：``消息：`` 那一行是聊天记录**卡片本身**那条消息的
  id（聊天记录命中时图片那条记录是**内层**，``get_msg`` 不认内层 id——实测一律
  "message not found"，拿它复搬必然取不回原消息）；
* **放行是一次性的**：``ImageAuditGate.approve`` 只对紧跟着的这次复搬生效——复搬时
  过滤门会再跑一遍，没有放行登记的话刚放行就又命中又拦截，死循环；
* **去重闸门跳过**：被拦的内容在拦截前指纹就被 reserve 进库了（拦截发生在
  ``forward_dedup_gate`` 之后），复搬再走去重只会得到「发过了」然后原地踏步，
  所以 ``dedup=False``——人工确认的内容本来就该直接搬；
* 只认**回复标签消息**：直接回复审计群里的转发卡片解析不出原始 id，会提示
  「请回复【过滤标签】那条消息」。
"""

from __future__ import annotations

import re

from bot.services.forward.image_gate import ImageAuditGate
from core.logging import get_logger

__all__ = ["audit_release", "parse_tag"]

log = get_logger("bot.commands.audit_release")

# QQ 消息 id 可以是负数（int64 回绕，SnowLuma 实测出过 -781141664 这种），
# 两个正则都必须允许负号——否则标签解析不到消息 id，复搬直接报废。
_GROUP_RE = re.compile(r"来源群：(-?\d+)")
_MESSAGE_RE = re.compile(r"消息：(-?\d+)")


def parse_tag(text: str) -> tuple[str, str]:
    """从【过滤标签】文本里解析 (来源群, 消息 id)；解析不出返回空串。"""
    text = str(text or "")
    if "【过滤标签】" not in text:
        return "", ""
    group = _GROUP_RE.search(text)
    message = _MESSAGE_RE.search(text)
    return (
        group.group(1) if group else "",
        message.group(1) if message else "",
    )


async def _fetch_message(api, message_id) -> dict:
    """按 id 取单条消息事件；拿不到返回空 dict。"""
    if api is None or not message_id:
        return {}
    try:
        res = await api.get_message(message_id)
    except Exception as exc:
        log.warning("get_msg(%s) 失败: %s", message_id, exc)
        return {}
    data = (res or {}).get("data")
    return data if isinstance(data, dict) and data.get("message") else {}


def _message_text(event: dict) -> str:
    segs = (event or {}).get("message") or []
    parts = []
    for seg in segs:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append(str((seg.get("data") or {}).get("text") or ""))
    raw = str((event or {}).get("raw_message") or "")
    return "\n".join(parts) or raw


async def _reply(mp, event, text: str) -> None:
    try:
        await mp.send_message(event, text)
    except Exception as exc:  # 回复失败只写日志，别把命令流程打挂
        log.warning("回复失败: %s", exc)


async def _repost(handler, event, tag_event) -> None:
    """真正执行复搬：放行 → 按自动搬运配置重搬 → 回报结果。"""
    mp, forward = handler.mp, handler.forward
    gate: ImageAuditGate = forward.image_gate
    group_id = event.get("group_id") or ""
    message_id = event.get("message_id") or ""
    if not gate.approve(group_id, message_id):
        await _reply(
            mp, tag_event,
            f"这条（{group_id}:{message_id}）已经放行过一次了，别重复复搬喵",
        )
        return
    kwargs = dict(getattr(handler.settings, "auto_forward_kwargs", {}) or {})
    kwargs["dedup"] = False  # 拦截前指纹已 reserve，再走去重只会原地踏步
    log.info("放行 %s:%s，开始重新搬运（dedup=off）", group_id, message_id)
    try:
        handled = await forward.forward(event=event, is_recover=False, **kwargs)
    except Exception as exc:
        log.exception("复搬 %s:%s 时搬运异常", group_id, message_id)
        await _reply(mp, tag_event, f"复搬失败：{exc}")
        return
    if handled:
        await _reply(mp, tag_event, "已放行并重新搬运")
    else:
        await _reply(
            mp, tag_event,
            "放行了，但这条不在自动搬运的监听范围里（类型 / 来源群不匹配），没有搬运",
        )


async def audit_release(handler, event) -> None:
    """「复搬」命令入口：审计群回复【过滤标签】消息触发。"""
    mp, api = handler.mp, handler.api
    settings = handler.settings
    config = (settings.get("image_filter") or {}) if settings else {}
    audit_group = str(config.get("audit_group_id") or "")
    tag_event = event
    if str(event.get("group_id")) != audit_group:
        # 审计群和别的群共用一条关键词时：别的群什么都不做（命令群已限定，
        # 这里是最后一道保险），防止普通群误触发搬运。
        return

    operators = [str(q) for q in (config.get("audit_operators") or []) if str(q)]
    sender = str(event.get("user_id") or "")
    if operators and sender not in operators:
        await _reply(mp, event, "只有审计员可以复搬喵")
        return

    reply_id = mp.is_reply(event)
    if not reply_id:
        await _reply(mp, event, "回复要放行的【过滤标签】那条消息，再发「复搬」喵")
        return
    replied = await _fetch_message(api, reply_id)
    group_id, message_id = parse_tag(_message_text(replied))
    if not group_id or not message_id:
        await _reply(
            mp, event,
            "回复的那条不是【过滤标签】消息（解析不到来源群 / 消息 id）。"
            "请回复带【过滤标签】开头的那条",
        )
        return

    original = await _fetch_message(api, message_id)
    if not original:
        await _reply(
            mp, event,
            f"取不回原消息（群 {group_id} 消息 {message_id}），"
            "可能已过期或被撤回，没法复搬"
            "（标签里记的是聊天记录内层 id 也会这样——旧版本打的标签要重新触发）",
        )
        return
    # 原消息所在的群以标签里写的为准（跨群消息 get_msg 不带 group_id 时兜底）
    original.setdefault("group_id", group_id)
    await _repost(handler, original, tag_event)
