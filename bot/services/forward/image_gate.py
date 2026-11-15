# -*- coding: utf-8 -*-
"""过滤命中后的审计标签：把命中的图 / 聊天记录发到审计群并注明类型。

和 :mod:`messaging.image_filter` 的分工：

* 那边只回答「命中什么」；
* 这边决定「怎么发」——单图用 ``single``（一条 ``[文本, 图片]``）还是
  ``forward_reply``（先转发原消息，再回复标签）；聊天记录用 ``reply``
  （先转发记录，再回复标签）/ ``before`` / ``after``。

所有发送都通过 ``OneBotAPI``，NapCat 与 SnowLuma 走同一套接口；后端差异
（文件段 ``name`` 等）已经在抽象层处理，这里不分支。
"""

from __future__ import annotations

import base64
import time
from collections import OrderedDict
from typing import Any, Iterable

from core.logging import get_logger
from messaging.image_filter import (
    FilterHit,
    ImageFilterHub,
    ImageProbe,
    normalize_filter_config,
)

__all__ = ["ImageAuditGate"]

log = get_logger("bot.services.forward.image_gate")


#: 审计群转发聊天记录时给足上传时间（和正式搬运一致）
AUDIT_FORWARD_TIMEOUT = 180.0
#: 一条标签消息里最多塞多少字节图片（base64 前），避免消息过大被拒
MAX_TAG_IMAGE_BYTES = 12 * 1024 * 1024
#: 已经打过的标签记一下，断开回放 / 同一内容多次触发时别重复刷审计群
_SEEN_LIMIT = 512
#: 审计群「复搬」放行登记的上限 / 有效期：够用就好，防无限膨胀
_APPROVED_LIMIT = 64
_APPROVED_TTL = 30 * 60  # 秒


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _sender_label(probe: ImageProbe) -> str:
    name = _as_text(probe.sender_name)
    user_id = _as_text(probe.sender_id)
    if name and user_id:
        return f"{name}（{user_id}）"
    return name or user_id or "未知"


def _locate_info(probe: ImageProbe) -> tuple[str, str]:
    """标签里的定位信息：(来源群, 消息 id)——复搬就靠它 ``get_msg`` 取回原消息。

    **必须写外层**（聊天记录卡片本身那条消息）的 id：

    * 聊天记录命中时 ``probe.message_id`` 是**内层**那条记录的 id。后端
      ``get_msg`` 只认外层消息——内层 id 实测一律 "message not found"
      （SnowLuma / NapCat 都一样），拿它复搬必然失败；
    * ``probe.event`` 才是外层卡片事件（生产路径
      ``screen_messages(messages, event=event)`` 传的就是它）；单图时
      event == message，两者一致，不受影响。

    拿不到外层时（老调用方没传 event）退回 probe 自己的字段，至少不是空。
    """
    event = probe.event or {}
    group_id = _as_text(event.get("group_id")) or _as_text(probe.group_id)
    message_id = _as_text(event.get("message_id")) or _as_text(probe.message_id)
    return group_id, message_id


def _format_tag_text(hits: list[FilterHit]) -> str:
    """标签正文：第一行必须是命中类型，后面才是定位信息。

    定位信息（``来源群：`` / ``消息：``）写的是**外层**消息——见
    :func:`_locate_info`。内层 id 放在详情行里（``[内层 xxxx]``），人工排查
    具体是哪条记录被拦时看它。
    """
    labels: list[str] = []
    for hit in hits:
        if hit.label not in labels:
            labels.append(hit.label)
    first = hits[0].probe if hits else ImageProbe()
    group_id, message_id = _locate_info(first)
    lines = [f"【过滤标签】{' / '.join(labels) or '未知类型'}"]
    if group_id:
        lines.append(f"来源群：{group_id}")
    if first.sender_id or first.sender_name:
        lines.append(f"发送者：{_sender_label(first)}")
    if message_id:
        lines.append(f"消息：{message_id}")
    lines.append("详情：")
    for index, hit in enumerate(hits, 1):
        probe = hit.probe
        score = f" score={hit.score:g}" if hit.score is not None else ""
        detail = f" {hit.detail}" if hit.detail else ""
        inner = _as_text(probe.message_id)
        # 标签正文里的 id 是外层；内层 id 只在跟外层不同时补一句，方便人工
        # 对回「记录里第几条」被拦了
        inner_mark = f" [内层 {inner}]" if inner and inner != message_id else ""
        lines.append(
            f"{index}. #{probe.index + 1} {hit.label}"
            f"（{hit.filter_id}）{score}{detail}{inner_mark}".rstrip()
        )
    return "\n".join(lines)


class ImageAuditGate:
    """图片命中 → 审计群标签。没有配置 / 没有 API 时整体安静停用。"""

    def __init__(
        self,
        *,
        settings=None,
        api=None,
        mp=None,
        hub: ImageFilterHub | None = None,
    ) -> None:
        self.settings = settings
        self.api = api
        self.mp = mp
        self.hub = hub or ImageFilterHub(settings=settings)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        # 审计群「复搬」放行登记：{key: 登记时间}。命中内容被人工确认后，
        # 复搬时会再过一遍过滤门——这里放行一次，否则刚放行就又命中又拦截。
        self._approved: "OrderedDict[str, float]" = OrderedDict()

    # ── 配置 ──
    def _get(self, key: str, default=None):
        getter = getattr(self.settings, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                pass
        if isinstance(self.settings, dict):
            return self.settings.get(key, default)
        return default

    @property
    def config(self) -> dict:
        return normalize_filter_config(self._get("image_filter"))

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enable"))

    def note_event(self, event) -> None:
        self.hub.note_event(event)

    def close(self) -> None:
        """释放图片门持有的下载缓存和推理资源。"""
        close = getattr(self.hub, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    # ── 审计群「复搬」放行 ──
    @staticmethod
    def _event_key(event: dict) -> str:
        event = event or {}
        return f"{event.get('group_id') or ''}:{event.get('message_id') or ''}"

    def approve(self, group_id, message_id) -> bool:
        """登记一条放行（审计群人工确认后复搬用）。重复登记返回 False。"""
        key = f"{group_id}:{message_id}"
        if key in self._approved:
            return False
        self._approved[key] = time.time()
        self._approved.move_to_end(key)
        while len(self._approved) > _APPROVED_LIMIT:
            self._approved.popitem(last=False)
        return True

    def _approved_now(self, event: dict) -> bool:
        """这条事件是否在有效期内被放行过；顺带清掉过期登记。"""
        key = self._event_key(event)
        marked = self._approved.get(key)
        if marked is None:
            return False
        if time.time() - marked > _APPROVED_TTL:
            self._approved.pop(key, None)
            return False
        # 一次性：放行只对紧跟着的这次复搬生效，防止放行消息反复触发搬运
        self._approved.pop(key, None)
        return True

    # ── 判定入口（给 ForwardService 调） ──
    async def screen_event(self, event: dict) -> list[FilterHit]:
        if not self.enabled:
            return []
        if self._approved_now(event):
            log.info(
                "单图 %s 已被审计群放行，本次跳过过滤直接搬运",
                event.get("message_id"),
            )
            return []
        return await self.hub.screen_event(event)

    async def screen_messages(
        self, messages: Iterable[dict], *, event: dict | None = None
    ) -> list[FilterHit]:
        if not self.enabled:
            return []
        if event is not None and self._approved_now(event):
            log.info(
                "聊天记录 %s 已被审计群放行，本次跳过过滤直接搬运",
                event.get("message_id"),
            )
            return []
        return await self.hub.screen_messages(messages, event=event)

    def load_image(self, url: str, timeout: float = 8.0) -> bytes | None:
        """给 ``build_forward_nodes`` 复用已经下载过的字节。"""
        return self.hub.load_image(url, timeout)

    # ── 标签发送 ──
    async def publish_event(self, event: dict, hits: list[FilterHit]) -> None:
        """单图命中：按 ``single_tag_mode`` 发标签。"""
        hits = self._fresh_hits("single_image", hits)
        if not hits:
            return
        config = self.config
        audit_group = _as_text(config.get("audit_group_id"))
        if not audit_group:
            log.warning("未配置 image_filter.audit_group_id，命中后无处打标签")
            return
        await self._ensure_connected()
        message = self._tag_message(hits)

        if config.get("single_tag_mode") == "forward_reply":
            message_id = await self._forward_event(event, audit_group=audit_group)
            if message_id:
                message = [self._reply_segment(message_id)] + message
            await self._send_group_message(audit_group, message)
        else:
            await self._send_group_message(audit_group, message)

    async def publish_record(
        self,
        event: dict,
        hits: list[FilterHit],
        *,
        nodes: list[dict] | None = None,
    ) -> None:
        """聊天记录命中：先把记录转发到审计群，再按 ``record_tag_mode`` 打标签。"""
        hits = self._fresh_hits("chat_record", hits)
        if not hits:
            return
        config = self.config
        audit_group = _as_text(config.get("audit_group_id"))
        if not audit_group:
            log.warning("未配置 image_filter.audit_group_id，命中后无处打标签")
            return
        await self._ensure_connected()
        mode = str(config.get("record_tag_mode") or "reply")
        tag = self._tag_message(hits)

        if mode == "before":
            await self._send_group_message(audit_group, tag)
            await self._forward_record(event, audit_group, nodes)

        elif mode == "after":
            await self._forward_record(event, audit_group, nodes)
            await self._send_group_message(audit_group, tag)

        else:  # reply
            message_id = await self._forward_record(event, audit_group, nodes)
            if message_id:
                tag = [self._reply_segment(message_id)] + tag
            await self._send_group_message(audit_group, tag)

    # ── 消息构造 ──
    def _tag_message(self, hits: list[FilterHit]) -> list[dict]:
        config = self.config
        message = [{"type": "text", "data": {"text": _format_tag_text(hits)}}]
        limit = int(config.get("max_tag_images") or 6)
        total = 0
        seen: set[str] = set()
        for hit in hits:
            probe = hit.probe
            key = probe.url or f"{probe.message_id}:{probe.index}"
            if key in seen:
                continue
            seen.add(key)
            segment = self._image_segment(probe, remaining=MAX_TAG_IMAGE_BYTES - total)
            if segment is None:
                continue
            size = len(probe.data or b"")
            total += size
            message.append(segment)
            if len(message) - 1 >= limit:
                break
        return message

    @staticmethod
    def _image_segment(probe: ImageProbe, *, remaining: int) -> dict | None:
        data = probe.data
        if data and len(data) <= remaining:
            return {
                "type": "image",
                "data": {
                    "file": "base64://" + base64.b64encode(data).decode("ascii"),
                    "summary": "过滤命中",
                },
            }
        if probe.url:
            return {
                "type": "image",
                "data": {"file": probe.url, "summary": "过滤命中"},
            }
        return None

    @staticmethod
    def _reply_segment(message_id: Any) -> dict:
        return {"type": "reply", "data": {"id": str(message_id)}}

    # ── 发送 ──
    async def _ensure_connected(self) -> None:
        ensure = getattr(self.mp, "ensure_connected", None)
        if callable(ensure):
            try:
                await ensure()
            except Exception as exc:
                log.warning("等待连接失败，继续尝试发送: %s", exc)

    async def _send_group_message(self, group_id: str, message: list[dict]) -> dict | None:
        if self.api is None:
            log.warning("没有 API，标签消息未发送")
            return None
        try:
            return await self.api.send_group_message(group_id=group_id, message=message)
        except Exception as exc:
            log.warning("发送标签到群 %s 失败: %s", group_id, exc)
            return None

    async def _forward_event(self, event: dict, *, audit_group: str) -> str:
        message_id = _as_text((event or {}).get("message_id"))
        if not message_id or self.api is None:
            return ""
        forward = getattr(self.api, "forward_group_single_msg", None)
        if not callable(forward):
            return ""
        try:
            result = await forward(group_id=audit_group, message_id=message_id)
        except Exception as exc:
            log.warning("转发原消息到审计群失败: %s", exc)
            return ""
        return self._response_message_id(result)

    async def _forward_record(
        self,
        event: dict,
        audit_group: str,
        nodes: list[dict] | None,
    ) -> str:
        if self.api is None:
            return ""
        if nodes:
            try:
                result = await self.api.send_group_forward_msg(
                    group_id=audit_group,
                    messages=nodes,
                    timeout=AUDIT_FORWARD_TIMEOUT,
                )
                message_id = self._response_message_id(result)
                if message_id:
                    return message_id
            except Exception as exc:
                log.warning("转发聊天记录到审计群失败: %s", exc)
        # 没有重建节点 / 重建发送失败 → 退回整卡转发，至少让审计群能看到原记录
        return await self._forward_event(event, audit_group=audit_group)

    @staticmethod
    def _response_message_id(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        data = result.get("data")
        if isinstance(data, dict):
            value = data.get("message_id")
            if value not in (None, "", 0, "0"):
                return str(value)
        value = result.get("message_id")
        return "" if value in (None, "", 0, "0") else str(value)

    # ── 去重 ──
    def _fresh_hits(self, source: str, hits: list[FilterHit]) -> list[FilterHit]:
        fresh: list[FilterHit] = []
        for hit in hits:
            probe = hit.probe
            key = "|".join(
                (
                    source,
                    str(probe.group_id),
                    str(probe.message_id),
                    str(probe.index),
                    probe.url,
                    hit.filter_id,
                )
            )
            if key in self._seen:
                continue
            self._seen[key] = None
            self._seen.move_to_end(key)
            while len(self._seen) > _SEEN_LIMIT:
                self._seen.popitem(last=False)
            fresh.append(hit)
        return fresh
