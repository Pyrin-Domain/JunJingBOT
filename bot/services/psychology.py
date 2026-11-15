# -*- coding: utf-8 -*-
"""心理分析：回复一条消息 / 一段聊天记录，让模型做一次心理侧写。

流程（和旧式关键词命令一致，唤醒完全靠关键词）：

    关键词命中 → 是不是回复消息？
        ├─ 是 → 取回被回复的消息
        │      ├─ 是聊天记录 → ForwardExpander 摊平 → 过滤门筛图 → 素材
        │      └─ 不是       → 单条消息 → 素材
        └─ 否 → 用当前消息本身（去掉唤醒词后还有内容才算）

素材统一交给 :class:`~agent.psychology.PsychologyAnalyzer`（独立模型、
独立图片策略），拿到文本后引用触发消息回复。

几个刻意为之的边界：

* **不抢主流程**：只在关键词命中时运行，且 ``enable=false`` 时连关键词
  都不注册——群里不会出现「功能没开却回一句未启用」。
* **过滤门只管「要发给模型的图」**：整段记录可能有几百张图，全量下载判定
  既慢又没必要；心理分析真正会外发的是前 ``max_images`` 张，先判这几张。
* **取不到内容就说取不到**：展开失败、消息过期、私聊被限制等都在群里回一句
  可读的提示，不静默失败（人对命令没反应会一直重试）。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from AbstractionLayer import ForwardExpander, build_node, extract_forward_id
from AbstractionLayer.segments import is_photo, sticker_name
from agent.psychology import (
    NodeDraft,
    PsychologyAnalyzer,
    PsychologyError,
    PsychologyUnavailable,
    PsychEntry,
    PsychImage,
    PsychMaterial,
)
from core.logging import get_logger
from messaging.image_filter import ImageFilterHub, ImageProbe, probes_from_event, probes_from_messages

__all__ = ["PsychologyService"]

log = get_logger("bot.services.psychology")

#: 一条回复最多多少字（超过就拆成多条；QQ 单条太长会被截断）
_REPLY_CHUNK = 700
#: 单次分析最多回几条，防止模型跑飞刷屏
_MAX_REPLY_CHUNKS = 3
#: 不去重地保留多少条历史，用来在「没回复」时提示用法
_INTRO = (
    "用法：回复一条消息或一段聊天记录，发「{keyword}」。\n"
    "我会只看这段内容本身做侧写，不会去翻别的聊天。"
)
#: 单张节点图片上限（base64 前）
_MAX_NODE_IMAGE_BYTES = 4 * 1024 * 1024
#: 一条聊天记录里图片的总预算（base64 前）；超了后面的图不再附，避免请求过大
_FORWARD_IMAGE_BUDGET = 12 * 1024 * 1024
#: 发送聊天记录的超时：协议端要等整份上传完才回执
_FORWARD_TIMEOUT = 180.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(minimum, number)


def _int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value if _text(item)]


def _sender_parts(message: dict) -> tuple[str, str]:
    """一条消息的 ``(QQ, 显示名)``；构造聊天记录节点时 node 用这两个字段。"""
    sender = message.get("sender")
    if isinstance(sender, dict):
        user_id = _text(sender.get("user_id"))
        name = _text(sender.get("card")) or _text(sender.get("nickname"))
        return user_id, name or user_id
    user_id = _text(message.get("user_id"))
    name = _text(message.get("nickname")) or _text(message.get("card"))
    return user_id, name or user_id


def _sender_of(message: dict) -> str:
    """聊天记录里一条消息的发送者显示名。"""
    user_id, name = _sender_parts(message)
    if name and user_id and name != user_id:
        return f"{name}（{user_id}）"
    return name or user_id or "未知"


def _time_value(message: dict) -> int:
    try:
        return int(float(message.get("time") or 0))
    except (TypeError, ValueError):
        return 0


def _time_of(message: dict) -> str:
    raw = message.get("time")
    if raw in (None, ""):
        return ""
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return _text(raw)[:16]
    # 有的后端给毫秒
    if stamp > 1e12:
        stamp /= 1000.0
    try:
        return datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _segments(message: dict) -> list[dict]:
    segs = message.get("message")
    if not segs:
        segs = message.get("content")
    return [seg for seg in (segs or []) if isinstance(seg, dict)]


def _normalize_message(message: Any) -> dict:
    """部分后端把子消息放在 ``content`` 里；统一成 ``message`` 段。

    过滤门的 ``iter_photos`` 只认 ``message`` 键，不统一的话会出现
    「渲染时有图、收集时没有」的错位。
    """
    if not isinstance(message, dict):
        return {}
    if message.get("message"):
        return message
    content = message.get("content")
    return {**message, "message": content} if content else message


def _render_segments(segments: list[dict], image_counter: list[int]) -> tuple[str, list[int]]:
    """把一条消息的段渲染成一行文本；普通图片占位成 ``[图片N]``。

    :return: ``(正文, 本条消息里的图片编号列表)``——编号是全局的（1-based），
        与 ``probes_from_messages`` 的顺序一致，后面构造节点时按它取图片。
    """
    parts: list[str] = []
    image_indexes: list[int] = []
    for seg in segments:
        kind = _text(seg.get("type"))
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(_text(data.get("text")))
        elif kind == "image":
            if is_photo(seg):
                image_counter[0] += 1
                image_indexes.append(image_counter[0])
                parts.append(f"[图片{image_counter[0]}]")
            else:
                name = sticker_name(seg)
                parts.append(f"[表情{name}]" if name else "[表情]")
        elif kind == "mface":
            name = _text(data.get("summary")).strip("[]【】()（）")
            parts.append(f"[表情{name}]" if name else "[表情]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "at":
            name = _text(data.get("name")) or _text(data.get("qq"))
            parts.append(f"@{name}" if name else "@某人")
        elif kind == "reply":
            quoted = _reply_summary(data)
            parts.append(f"<回复消息<{quoted}>>" if quoted else "[回复]")
        elif kind == "forward":
            parts.append("[聊天记录]")
        elif kind == "json":
            parts.append("[卡片]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "file":
            parts.append("[文件]")
        else:
            parts.append(f"[{kind or '未知'}]")
    return "".join(parts).strip(), image_indexes


def _reply_summary(data: dict) -> str:
    """引用段里带的被引用内容（有就写进节点，像 ``<回复消息<...>>``）。"""
    message = data.get("message")
    if isinstance(message, list):
        pieces = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                pieces.append(_text((seg.get("data") or {}).get("text")))
        text = "".join(pieces).strip()
        if text:
            return text[:60]
    return _text(data.get("id"))[:20]


def _split_reply(text: str, limit: int = _REPLY_CHUNK) -> list[str]:
    """按段落切成长度合适的多条消息，尽量不把一句话切断。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                current += "\n"
            continue
        if len(current) + len(line) + 1 <= limit:
            current = f"{current}\n{line}" if current else line
            continue
        if current:
            chunks.append(current.strip())
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current.strip():
        chunks.append(current.strip())
    return chunks[:_MAX_REPLY_CHUNKS]


def _image_segment(image: PsychImage) -> tuple[dict | None, int]:
    """节点里的图片段：优先 base64（跨机 / rkey 过期都稳），太大退回 URL。

    :return: ``(消息段, 计入预算的字节数)``
    """
    data = image.data or b""
    if data and len(data) <= _MAX_NODE_IMAGE_BYTES:
        return (
            {
                "type": "image",
                "data": {
                    "file": "base64://" + base64.b64encode(data).decode("ascii"),
                    "summary": "图片",
                },
            },
            len(data),
        )
    if image.url:
        return ({"type": "image", "data": {"file": image.url, "summary": "图片"}}, 0)
    return (None, 0)


@dataclass
class _Target:
    """定位到的分析对象。"""

    messages: list[dict]          # 要分析的消息（聊天记录 = 展开后的叶子）
    source: str                   # reply_record / reply_single / current
    title: str
    record_event: dict            # 过滤门要的外层事件（定位 / 审计都用外层）
    truncated: bool = False


class PsychologyService:
    """关键词「分析心理」的业务编排。"""

    def __init__(
        self,
        *,
        mp,
        api=None,
        settings=None,
        forward=None,
        analyzer: Optional[PsychologyAnalyzer] = None,
        ocr_text: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self.mp = mp
        self.api = api
        self.settings = settings
        self.forward = forward
        self.analyzer = analyzer or PsychologyAnalyzer(
            settings, ocr_text=ocr_text or getattr(mp, "ocr_text", None)
        )
        self._last_run: dict[str, float] = {}
        self._running: set[str] = set()

    # ════════════════════ 配置 ════════════════════
    def _section(self) -> dict:
        if self.settings is None:
            return {}
        try:
            section = self.settings.section("psychology")
        except Exception:  # noqa: BLE001 - 老配置没有这个段
            return {}
        return section if isinstance(section, dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(self._section().get("enable"))

    @property
    def keywords(self) -> list[str]:
        words = _str_list(self._section().get("keywords"))
        return words or ["分析心理", "心理分析", "心理侧写"]

    @property
    def cooldown(self) -> float:
        return _num(self._section().get("cooldown_seconds"), 30, 0)

    @property
    def use_filter(self) -> bool:
        return bool(self._section().get("use_image_filter", True))

    def allowed(self, event: dict) -> bool:
        """群白名单 / 黑名单。私聊不受 groups 限制。"""
        section = self._section()
        if event.get("message_type") != "group":
            return bool(section.get("allow_private", True))
        group_id = _text(event.get("group_id"))
        blacklist = _str_list(section.get("blacklist_groups"))
        if group_id and group_id in blacklist:
            return False
        groups = _str_list(section.get("groups"))
        return not groups or group_id in groups

    def reload(self) -> None:
        """配置保存后的热加载：清掉冷却之外的状态。"""
        if hasattr(self.analyzer, "reload"):
            self.analyzer.reload()
        self._last_run.clear()

    # ════════════════════ 关键词 ════════════════════
    def keyword_pattern(self) -> Optional[dict]:
        """给 ``MessageProcessor.pattern_match`` 的规则。

        整条消息必须基本等于唤醒词（允许前后空白和标点）——「我今天想分析心理
        学」这种聊天不该把 Bot 拽出来。
        """
        if not self.enabled:
            return None
        words = sorted({word for word in self.keywords if word}, key=len, reverse=True)
        if not words:
            return None
        alternation = "|".join(re.escape(word) for word in words)
        return {
            "solution": self.handle,
            "regex": rf"^\s*(?:{alternation})[\s!！。.~～?？,，、]*$",
        }

    # ════════════════════ 入口 ════════════════════
    async def handle(self, event: dict) -> None:
        """关键词回调（由 ``MessageProcessor`` 后台调度，不阻塞主流程）。"""
        await self.run(event)

    async def run(
        self,
        event: dict,
        *,
        callback: Optional[Callable[[dict, PsychMaterial], Awaitable[str]]] = None,
    ) -> Optional[str]:
        """跑完整流程。

        :param callback: 素材准备好之后的回调；缺省 = 调模型并回复。
            留给别的模块复用（例如以后把素材接到别的地方），返回值仅用于测试。
        """
        if not event or not self.enabled:
            return None
        if not self.allowed(event):
            log.info("[心理分析] 群 %s 不在允许范围，跳过", event.get("group_id"))
            return None

        thread = self._thread_of(event)
        now = time.monotonic()
        last = self._last_run.get(thread, 0.0)
        if self.cooldown and now - last < self.cooldown:
            log.info(
                "[心理分析] %s 冷却中（%.0fs），忽略这次触发",
                thread,
                self.cooldown - (now - last),
            )
            return None
        if thread in self._running:
            log.info("[心理分析] %s 上一次分析还没跑完，忽略这次触发", thread)
            return None

        self._running.add(thread)
        self._last_run[thread] = now
        try:
            material = await self._collect(event)
            if material is None:
                return None
            log.info("[心理分析] %s → %s", thread, material.summary())
            if callback is not None:
                return await callback(event, material)
            return await self._analyze_and_reply(event, material)
        except Exception as exc:  # noqa: BLE001 - 兜底，别让后台任务静默死掉
            log.exception("[心理分析] 执行失败")
            await self._reply(event, f"心理分析出错了：{exc.__class__.__name__}: {exc}")
            return None
        finally:
            self._running.discard(thread)

    # ════════════════════ 收集素材 ════════════════════
    async def _collect(self, event: dict) -> Optional[PsychMaterial]:
        target = await self._locate(event)
        if target is None:
            return None
        material = self._build_material(target)
        await self._attach_images(target, material)
        return material

    async def _locate(self, event: dict) -> Optional[_Target]:
        """回复的消息 > 当前消息；是聊天记录就展开。"""
        reply_id = self.mp.is_reply(event)
        if reply_id:
            target_event = await self._get_message(reply_id)
            if not target_event:
                await self._reply(
                    event,
                    "取不到你回复的那条消息（可能已经过期 / 被撤回），"
                    "试试直接回复那条消息再发一次。",
                )
                return None
            if extract_forward_id(target_event):
                return await self._locate_record(event, target_event)
            return _Target(
                messages=[_normalize_message(target_event)],
                source="reply_single",
                title=f"被回复的消息（{_sender_of(target_event)}）",
                record_event=target_event,
            )

        # 没回复：用当前消息本身，但要求除了唤醒词还有内容
        if self._is_bare_command(event):
            await self._reply(event, _INTRO.format(keyword=self.keywords[0]))
            return None
        return _Target(
            messages=[_normalize_message(event)],
            source="current",
            title=f"当前消息（{_sender_of(event)}）",
            record_event=event,
        )

    async def _locate_record(self, event: dict, record_event: dict) -> Optional[_Target]:
        messages, diag = await self._expand_record(record_event)
        if not messages:
            await self._reply(
                event,
                f"这条聊天记录我打不开（{diag}）。可能是内容太大或已过期，"
                "试试只回复其中一小段。",
            )
            return None
        sender = _sender_of(record_event)
        return _Target(
            messages=[_normalize_message(message) for message in messages],
            source="reply_record",
            title=f"被回复的聊天记录（{len(messages)} 条，来自 {sender}）",
            record_event=record_event,
            truncated=len(messages) >= self._max_records(),
        )

    def _max_records(self) -> int:
        return _int(self._section().get("max_records"), 60, 1)

    async def _expand_record(self, record_event: dict) -> tuple[list[dict], str]:
        """展开聊天记录：抽象层摊平 → 单层 ``get_forward_msg`` 兜底。"""
        kernel = getattr(self.api, "kernel", None)
        if kernel is not None:
            try:
                expander = ForwardExpander(kernel, max_items=self._max_records())
                forward_id = extract_forward_id(record_event)
                expansion = (
                    await expander.expand(forward_id)
                    if forward_id
                    else await expander.expand_event(record_event)
                )
            except Exception as exc:  # noqa: BLE001 - 展开失败退回单层
                log.warning("[心理分析] 抽象层展开失败: %s", exc)
            else:
                if expansion.messages:
                    return list(expansion.messages), f"expand[{expansion.describe()}]"
                if expansion.errors:
                    log.warning(
                        "[心理分析] 展开没有内容（%s），退回单层",
                        "；".join(expansion.errors),
                    )

        candidates: list = []
        forward_id = extract_forward_id(record_event)
        if forward_id:
            candidates.append(forward_id)
        if record_event.get("message_id"):
            candidates.append(record_event["message_id"])
        seen: set[str] = set()
        for message_id in candidates:
            key = _text(message_id)
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                await self.mp.ensure_connected()
                res = await self.api.get_forward_msg({"message_id": message_id})
            except Exception as exc:  # noqa: BLE001
                log.warning("[心理分析] get_forward_msg(%s) 失败: %s", key, exc)
                continue
            status = _text((res or {}).get("status")) or "none"
            messages = ((res or {}).get("data") or {}).get("messages") or []
            if status != "failed" and messages:
                normalized = [
                    {**item, "message": item.get("message") or item.get("content") or []}
                    for item in messages
                    if isinstance(item, dict)
                ]
                return normalized[: self._max_records()], f"get_forward_msg[{key}]"
        return [], f"展开 + {len(seen)} 个 id 的单层查询都失败"

    async def _get_message(self, message_id: Any) -> dict:
        if self.api is None or not message_id:
            return {}
        try:
            await self.mp.ensure_connected()
            res = await self.api.get_message(message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[心理分析] get_msg(%s) 失败: %s", message_id, exc)
            return {}
        data = (res or {}).get("data")
        if not isinstance(data, dict) or not (data.get("message") or data.get("content")):
            return {}
        return data

    def _is_bare_command(self, event: dict) -> bool:
        """当前消息除了唤醒词没有别的内容（且没有图）。"""
        has_image = bool(probes_from_event(event))
        text = ""
        for seg in _segments(event):
            if seg.get("type") == "text":
                text += _text((seg.get("data") or {}).get("text"))
        for word in self.keywords:
            text = re.sub(re.escape(word), "", text)
        text = re.sub(r"[\s!！。.~～?？,，、]+", "", text)
        return not has_image and len(text) < 2

    def _build_material(self, target: _Target) -> PsychMaterial:
        """把消息渲染成文本，同时登记逐条原文和图片占位。"""
        lines: list[str] = []
        images: list[PsychImage] = []
        entries: list[PsychEntry] = []
        counter = [0]
        for message in target.messages:
            if not isinstance(message, dict):
                continue
            sender = _sender_of(message)
            sender_id, sender_name = _sender_parts(message)
            when = _time_of(message)
            segments = _segments(message)
            body, image_indexes = _render_segments(segments, counter)
            if not body:
                continue
            prefix = f"[{when}] " if when else ""
            lines.append(f"{prefix}{sender}：{body}")
            entries.append(
                PsychEntry(
                    index=len(entries) + 1,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    time=_time_value(message),
                    text=body,
                    image_indexes=image_indexes,
                )
            )
            for index in image_indexes:
                images.append(
                    PsychImage(
                        label=f"#{index}",
                        sender=sender,
                        caption=lines[-1][:80],
                    )
                )
        transcript = "\n".join(lines)
        return PsychMaterial(
            source=target.source,
            title=target.title,
            transcript=transcript,
            images=images,
            entries=entries,
            truncated=target.truncated,
        )

    async def _attach_images(self, target: _Target, material: PsychMaterial) -> None:
        """下载要发给模型的图片，并过一遍图片过滤门。"""
        section = self._section()
        # 默认 6 张；显式写 0 才表示「不带图」
        limit = _int(section.get("max_images"), 6, 0)
        if not material.images:
            return
        if limit == 0:
            material.skipped = len(material.images)
            material.images = []
            return

        probes = probes_from_messages(
            target.messages,
            event=target.record_event,
            source="psychology",
            max_images=limit,
        )
        if len(material.images) > limit:
            material.skipped += len(material.images) - limit
            material.images = material.images[:limit]
        if not probes:
            return

        hub = self._hub()
        try:
            await hub.download_probes(probes)
        except Exception as exc:  # noqa: BLE001 - 下载失败只是没图可用
            log.warning("[心理分析] 图片下载失败: %s", exc)
        hits: list = []
        if self.use_filter:
            try:
                hits = await _evaluate(hub, probes)
            except Exception as exc:  # noqa: BLE001 - 过滤失败按「没命中」处理
                log.warning("[心理分析] 图片过滤失败: %s", exc)
                hits = []
        hit_probes = {id(hit.probe) for hit in hits}
        hit_urls = {hit.probe.url for hit in hits if hit.probe.url}

        for image, probe in zip(material.images, probes):
            image.url = _text(probe.url) or image.url
            image.data = probe.data
            if not probe.data:
                image.note = probe.error or "下载失败"
            # probe 对象是同一批（evaluate 直接在原列表上判），优先按对象身份匹配；
            # 拿不到对象时再退回 url（本地缓存命中的 probe 可能没有 url）。
            if id(probe) in hit_probes or (probe.url and probe.url in hit_urls):
                image.blocked = True
                image.note = "图片过滤命中，未发送给模型"
        for image in material.images[len(probes):]:
            image.note = image.note or "没取到图片地址"
        blocked = sum(1 for image in material.images if image.blocked)
        if blocked:
            log.info("[心理分析] 过滤门拦下 %s 张图（不发给模型）", blocked)

    def _hub(self) -> ImageFilterHub:
        """复用搬运链路的图片过滤门（同一份下载缓存 / 模型实例）。"""
        gate = getattr(self.forward, "image_gate", None)
        hub = getattr(gate, "hub", None)
        if hub is not None:
            return hub
        cached = getattr(self, "_orphan_hub", None)
        if cached is None:
            cached = ImageFilterHub(settings=self.settings)
            self._orphan_hub = cached
        return cached

    # ════════════════════ 调模型 + 回复 ════════════════════
    def _reply_mode(self) -> str:
        """回复形式：``text`` / ``forward`` / ``both``，默认聊天记录。"""
        mode = _text(self._section().get("reply_mode")).lower()
        return mode if mode in ("text", "forward", "both") else "forward"

    async def _analyze_and_reply(self, event: dict, material: PsychMaterial) -> str:
        mode = self._reply_mode()
        if mode == "text":
            return await self._reply_text(event, material)
        if mode == "both":
            answer = await self._reply_text(event, material)
            await self._reply_forward(event, material)
            return answer
        return await self._reply_forward(event, material)

    async def _reply_text(self, event: dict, material: PsychMaterial) -> str:
        try:
            answer = await self.analyzer.analyze(material)
        except PsychologyUnavailable as exc:
            log.warning("[心理分析] %s", exc)
            await self._reply(event, f"{exc}（面板 → 心理分析 里配置）")
            return ""
        except PsychologyError as exc:
            log.warning("[心理分析] %s", exc)
            await self._reply(event, str(exc))
            return ""
        await self._reply(event, answer)
        return answer

    async def _reply_forward(self, event: dict, material: PsychMaterial) -> str:
        """把逐条侧写发成一条聊天记录（和 message_id 那条示例一样的形态）。"""
        if not material.entries:
            return await self._reply_text(event, material)
        try:
            drafts = await self.analyzer.analyze_nodes(material)
        except PsychologyUnavailable as exc:
            log.warning("[心理分析] %s", exc)
            await self._reply(event, f"{exc}（面板 → 心理分析 里配置）")
            return ""
        except PsychologyError as exc:
            log.warning("[心理分析] 逐条侧写失败，退回文本模式: %s", exc)
            await self._reply(event, f"{exc}（这次改成一段文字侧写）")
            return await self._reply_text(event, material)

        if not any(draft.from_model for draft in drafts):
            log.warning("[心理分析] 逐条侧写没解析出来，退回文本模式")
            await self._reply(event, "这次模型没按逐条格式返回，我改成一段侧写：")
            return await self._reply_text(event, material)

        nodes = self._build_nodes(material, drafts)
        try:
            await self.mp.ensure_connected()
            if event.get("message_type") == "private":
                await self.api.send_private_forward_msg(
                    user_id=event["user_id"], messages=nodes, timeout=_FORWARD_TIMEOUT
                )
            else:
                await self.api.send_group_forward_msg(
                    group_id=event["group_id"], messages=nodes, timeout=_FORWARD_TIMEOUT
                )
        except Exception as exc:  # noqa: BLE001 - 发送失败至少把内容发成文字
            log.warning("[心理分析] 聊天记录发送失败，改发文字: %s", exc)
            await self._reply(event, "\n".join(draft.text for draft in drafts))
            return ""
        log.info("[心理分析] 已发送聊天记录：%s 条节点", len(nodes))
        return "\n".join(draft.text for draft in drafts)

    def _build_nodes(self, material: PsychMaterial, drafts: list[NodeDraft]) -> list[dict]:
        """把「原始条目 + 模型逐条侧写」拼成 ``send_*_forward_msg`` 的 node。"""
        by_index = {draft.index: draft.text for draft in drafts}
        include_images = bool(self._section().get("forward_include_images", True))
        budget = _FORWARD_IMAGE_BUDGET
        nodes: list[dict] = []
        for entry in material.entries:
            text = _text(by_index.get(entry.index)) or entry.text or "[消息]"
            content: list[dict] = [{"type": "text", "data": {"text": text}}]
            if include_images:
                for index in entry.image_indexes:
                    if budget <= 0:
                        break
                    image = (
                        material.images[index - 1]
                        if 0 < index <= len(material.images)
                        else None
                    )
                    if image is None or image.blocked or not image.data:
                        continue
                    segment, size = _image_segment(image)
                    if segment is None:
                        continue
                    content.append(segment)
                    budget -= size
            nodes.append(
                build_node(
                    content,
                    uin=entry.sender_id or "0",
                    name=entry.sender_name or "群友",
                    time=entry.time,
                )
            )
        return nodes

    async def _reply(self, event: dict, text: str) -> None:
        chunks = _split_reply(text)
        if not chunks:
            return
        reply_id = _text(event.get("message_id"))
        for index, chunk in enumerate(chunks):
            message: list[dict] = []
            if index == 0 and reply_id:
                message.append({"type": "reply", "data": {"id": reply_id}})
            message.append({"type": "text", "data": {"text": chunk}})
            try:
                await self.mp.ensure_connected()
                if event.get("message_type") == "private":
                    await self.api.send_private_message(
                        user_id=event["user_id"], message=message
                    )
                else:
                    await self.api.send_group_message(
                        group_id=event["group_id"], message=message
                    )
            except Exception as exc:  # noqa: BLE001 - 发送失败只记日志
                log.warning("[心理分析] 回复发送失败: %s", exc)
                log.info("[心理分析] 未发出的内容: %s", chunk)
                return

    @staticmethod
    def _thread_of(event: dict) -> str:
        if event.get("message_type") == "group":
            return f"group_{event.get('group_id')}"
        return f"private_{event.get('user_id')}"


async def _evaluate(hub, probes: list[ImageProbe]) -> list:
    """跑过滤门的判定（同步实现放到线程里，和搬运链路一致）。"""
    return await asyncio.to_thread(hub.evaluate, probes, source="psychology")
