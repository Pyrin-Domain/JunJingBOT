"""搬运抽象：把一张聊天记录卡片转投到别的群 / 人。

实测差异（2026-10-26，NapCat :3000 / SnowLuma :3210，同一个 QQ 号的两个协议端）：

把「外层 5 条、其中第 1 条本身又是一张聊天记录」的卡片搬到另一个群：

=============  ====================================  ====================================
方式            NapCat                                SnowLuma
=============  ====================================  ====================================
整卡搬运        ✅ json ``com.tencent.multimsg``，      ❌ 内层被压成 ``xml viewMultiMsg``，
``forward_      news 里第一条是 ``[聊天记录]``，        ``m_fileName`` 指向源群的临时文件，
single_msg``    可以点开                                跨群后失效，接收方看到 ``[XML消息]``
重建 +          ✅ 同整卡                                ✅ 保留 ``forward`` 段
``{"type":
"forward"}`` 段
重建 + 嵌套     ⚠️ 退化成 ``[卡片消息]``                ✅ 生成真正的双层聊天记录
``node``
=============  ====================================  ====================================

所以「搬家」不能只按一种方式做：

- 后端能整卡保真（NapCat）：优先整卡，原始 uin / 时间 / 头像都不动；
- 后端会降级（SnowLuma）：只要内层含嵌套聊天记录，就必须重建成嵌套 node，
  把内层内容**内联进去**（不依赖源群缓存，换设备 / 换群都还能打开）。

本模块只做判定和 node 构造，发不发由上层决定。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from .kernal import BackendCapabilities

__all__ = [
    "ForwardRelay",
    "RelayContent",
    "RelayPlan",
    "build_node",
    "has_nested_forward",
    "has_opaque_nested",
    "is_opaque_multimsg",
    "plan_relay",
    "segments_of",
    "titles_of_multimsg",
]

#: 认得出「这是一张聊天记录卡片、但拿不到里面内容」的标记。
_MULTIMSG_MARKERS = (
    'action="viewMultiMsg"',
    'm_fileName="MultiMsg',
    "com.tencent.multimsg",
    "MultiMsg_",
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _raw_of(segment: dict) -> str:
    """xml / json 段的原始字符串。OneBot 里 xml 段是 ``{"data": {"data": "…"}}``。"""
    data = segment.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("data", "content", "xml", "raw"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def is_opaque_multimsg(segment: Any) -> bool:
    """这个段是不是「拿不到内容的聊天记录」（xml/json 的多消息卡片）。

    这类段只有摘要（``<title>`` 列表），没有可展开的 id，整卡搬到别的会话
    后 ``m_fileName`` 指向源会话的临时文件，接收方只能看到「[XML消息]」。
    """
    if not isinstance(segment, dict):
        return False
    if segment.get("type") not in ("xml", "json"):
        return False
    raw = _raw_of(segment)
    return any(marker in raw for marker in _MULTIMSG_MARKERS)


def titles_of_multimsg(segment: Any) -> list[str]:
    """从多消息卡片里提取每一行的摘要文本。

    第一条通常是大标题（``xxx的聊天记录``），调用方一般要跳过。
    """
    raw = _raw_of(segment) if isinstance(segment, dict) else str(segment or "")
    if not raw:
        return []
    out: list[str] = []
    for chunk in _TITLE_RE.findall(raw):
        text = html.unescape(_TAG_RE.sub("", chunk)).strip()
        if text:
            out.append(text)
    return out


def segments_of(message: Any) -> list[dict]:
    """取一条消息的段数组；兼容 ``{"message": [...]}`` 与裸数组。"""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    if not isinstance(message, dict):
        return []
    value = message.get("message")
    if isinstance(value, list):
        return [s for s in value if isinstance(s, dict)]
    return []


def has_nested_forward(messages: Any) -> bool:
    """这批消息里有没有「聊天记录套聊天记录」（可展开的真 forward 段）。"""
    for message in messages or []:
        for segment in segments_of(message):
            if segment.get("type") == "forward":
                return True
    return False


def has_opaque_nested(messages: Any) -> bool:
    """这批消息里有没有「打不开的聊天记录」（xml/json 多消息卡片）。"""
    for message in messages or []:
        for segment in segments_of(message):
            if is_opaque_multimsg(segment):
                return True
    return False


def _forward_id_of(segment: dict) -> str:
    data = segment.get("data") or {}
    for key in ("id", "resid", "forward_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _inline_content_of(segment: dict) -> list[dict]:
    data = segment.get("data") or {}
    value = data.get("content")
    return [s for s in value if isinstance(s, dict)] if isinstance(value, list) else []


def _sender_of(message: dict) -> tuple[str, str, int]:
    sender = (message or {}).get("sender") or {}
    uin = str(sender.get("user_id") or message.get("user_id") or "0")
    name = str(sender.get("card") or sender.get("nickname") or "QQ用户")
    try:
        stamp = int(message.get("time") or 0)
    except (TypeError, ValueError):
        stamp = 0
    return uin, name, stamp


def build_node(
    content: list[dict],
    *,
    uin: Any = "0",
    name: Any = "QQ用户",
    time: Any = 0,
) -> dict:
    """一个 ``send_group_forward_msg`` 的 node。"""
    try:
        stamp = int(time or 0)
    except (TypeError, ValueError):
        stamp = 0
    return {
        "type": "node",
        "data": {
            "uin": str(uin or "0"),
            "name": str(name or "QQ用户"),
            "time": stamp,
            "content": content,
        },
    }


def node_from_message(message: dict) -> dict | None:
    """一条消息 → node；没有可发送的段就返回 None。"""
    segments = segments_of(message)
    if not segments:
        return None
    uin, name, stamp = _sender_of(message)
    return build_node(segments, uin=uin, name=name, time=stamp)


# ══════════════════════════════════════════════════════════
# 搬运方案
# ══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RelayPlan:
    """搬运策略。

    ``action``：

    - ``single``：``forward_group_single_msg`` 整卡搬，最保真；
    - ``rebuild``：重建 node 走 ``send_group_forward_msg``。
    """

    action: str
    reason: str
    nested: bool = False
    #: 有没有拿不到内容的多消息卡片（只能降级成摘要文本）
    opaque: bool = False

    @property
    def needs_rebuild(self) -> bool:
        return self.action == "rebuild"

    @property
    def lossy(self) -> bool:
        """重建会不会丢东西（内层卡片只能退化成摘要文本）。"""
        return self.opaque and self.needs_rebuild


def plan_relay(
    capabilities: BackendCapabilities,
    messages: list[dict],
    *,
    force_rebuild: bool = False,
) -> RelayPlan:
    """决定这张卡片该怎么搬。

    :param capabilities: 当前后端能力
    :param messages: 卡片**内层**的消息（调用方先 ``get_forward_msg`` 拉一层）
    :param force_rebuild: 上层因为裁剪 / 追加已经决定重建时传 True
    """
    nested = has_nested_forward(messages)
    opaque = has_opaque_nested(messages)

    if force_rebuild:
        return RelayPlan("rebuild", "调用方要求重建（裁剪 / 追加）", nested, opaque)

    # 内层已经是 xml/json 死卡片：m_fileName 指向**源会话**的临时多消息文件。
    # 这张卡在目标会话里没有任何可用资源引用，任何后端「整卡重发」都只会再发一次
    # 打不开的卡（手机端能进、图片加载失败），因此与后端能力无关，一律重建。
    if opaque:
        return RelayPlan(
            "rebuild",
            "内层是 xml/json 聊天记录卡，其 m_fileName 指向源会话来时文件，"
            "换会话后必然失效；重建后至少摘要内容可读",
            nested,
            opaque,
        )

    if nested and not capabilities.relay_single_preserves_nested:
        return RelayPlan(
            "rebuild",
            f"{capabilities.label} 整卡搬运会把内层聊天记录压成 XML，"
            "必须重建才能保证接收方打得开",
            nested,
            opaque,
        )

    if nested:
        return RelayPlan("single", "后端整卡搬运会按目标会话重建 resid，嵌套记录可打开", nested, opaque)
    return RelayPlan("single", "内层没有嵌套转发，整卡搬运即可", nested, opaque)


# ══════════════════════════════════════════════════════════
# 重建
# ══════════════════════════════════════════════════════════
@dataclass
class RelayContent:
    """重建结果。"""

    nodes: list[dict] = field(default_factory=list)
    #: 拉到过的内层转发 id（诊断用）
    expanded: list[str] = field(default_factory=list)
    #: 展开失败 / 超限的 id
    unresolved: list[str] = field(default_factory=list)
    #: 能拿到内容、正常内联的层
    errors: list[str] = field(default_factory=list)
    #: 拿不到内容、只能降级成摘要的聊天记录标题
    degraded: list[str] = field(default_factory=list)
    depth_reached: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.nodes)

    @property
    def lossy(self) -> bool:
        """这次重建有没有丢东西（有内层卡片只能退化成摘要）。"""
        return bool(self.degraded)


class ForwardRelay:
    """按后端能力把聊天记录重建成 node 列表。

    关键点：遇到内层 ``forward`` 段时**不保留原 id**，而是把它的内容拉下来
    内联成嵌套 node。这样接收方打开时不依赖源群 / 源会话的缓存。

        relay = ForwardRelay(kernel)
        content = await relay.collect(forward_id)
        await api.send_group_forward_msg(group_id=..., messages=content.nodes)
    """

    def __init__(
        self,
        kernel,
        *,
        max_depth: int = 3,
        max_items: int = 500,
    ) -> None:
        self.kernel = kernel
        self.max_depth = max_depth
        self.max_items = max_items

    @property
    def capabilities(self) -> BackendCapabilities:
        return self.kernel.capabilities

    async def collect(
        self,
        forward_id: Any = None,
        *,
        messages: list[dict] | None = None,
    ) -> RelayContent:
        """把一张聊天记录重建成 node 列表。

        :param forward_id: 源卡片 id（``messages`` 没给时用它拉内容）
        :param messages: 已经拉好的顶层内容（调用方为避免重复请求时传入）
        """
        result = RelayContent()

        top: list[dict] = []
        if messages:
            top = [m for m in messages if isinstance(m, dict)]
        elif forward_id:
            top = await self._fetch(str(forward_id), result, level=1)
        else:
            result.errors.append("既没有 forward id 也没有内容")
            return result

        for message in top:
            if len(result.nodes) >= self.max_items:
                result.errors.append(f"超过 max_items={self.max_items}，已截断")
                break
            node = await self._node_of(message, result, level=1)
            if node is not None:
                result.nodes.append(node)
        return result

    async def _node_of(
        self,
        message: dict,
        result: RelayContent,
        *,
        level: int,
    ) -> dict | None:
        segments = segments_of(message)
        if not segments:
            return None

        content: list[dict] = []
        for segment in segments:
            seg_type = segment.get("type")
            if seg_type == "forward":
                # 一个 forward 段可能展开成多个 node（内层每条消息一个），全部塞进 content。
                content.extend(await self._nested_segments(segment, result, level=level))
            elif is_opaque_multimsg(segment):
                # 拿不到内容的聊天记录（xml/json 多消息卡片）→ 降级成摘要文本
                content.extend(self._opaque_segments(segment, result))
            else:
                content.append(segment)
            if len(content) >= self.max_items:
                result.errors.append(f"超过 max_items={self.max_items}，已截断")
                content = content[: self.max_items]
                break

        if not content:
            return None
        uin, name, stamp = _sender_of(message)
        return build_node(content, uin=uin, name=name, time=stamp)

    async def _nested_segments(
        self,
        segment: dict,
        result: RelayContent,
        *,
        level: int,
    ) -> list[dict]:
        """把一个内层 ``forward`` 段变成要插进 content 的段列表。

        - 后端认嵌套 node（SnowLuma）：展开成若干 node，每条消息一个，
          发送者 / 时间都保留，接收方看到的是真正的第二层聊天记录；
        - 后端只认 forward 段（NapCat）：留 ``{"type":"forward","data":{"id":…}}``，
          它自己会把内层内容内联进去。
        """
        inline = _inline_content_of(segment)
        forward_id = _forward_id_of(segment)
        next_level = level + 1

        if next_level > self.max_depth:
            result.errors.append(f"嵌套超过 max_depth={self.max_depth}")
            return self._fallback_segments(forward_id, result)

        messages: list[dict] = []
        if inline:
            messages = inline
        elif forward_id and self.capabilities.forward_inner_id_fetchable:
            messages = await self._fetch(forward_id, result, level=next_level)
        elif forward_id and self.capabilities.forward_inline_nested:
            # NapCat：fetch 不到内层 id，但会内联 content；这里没有 content 就留 id。
            messages = []

        if not messages:
            return self._fallback_segments(forward_id, result)

        result.depth_reached = max(result.depth_reached, next_level)

        if self.capabilities.relay_accepts_nested_node:
            inner: list[dict] = []
            for message in messages:
                node = await self._node_of(message, result, level=next_level)
                if node is not None:
                    inner.append(node)
                if len(inner) >= self.max_items:
                    result.errors.append(f"内层超过 max_items={self.max_items}，已截断")
                    break
            if inner:
                return inner

        if self.capabilities.relay_accepts_forward_segment and forward_id:
            return [{"type": "forward", "data": {"id": forward_id}}]

        return self._fallback_segments(forward_id, result)

    def _opaque_segments(self, segment: dict, result: RelayContent) -> list[dict]:
        """拿不到内容的聊天记录 → 摘要文本。

        SnowLuma 收到别人发的合并转发时，本地存的是 ``xml(viewMultiMsg)``，
        里面只有一行行 ``<title>`` 摘要，没有可展开的 id。跨群搬时
        ``m_fileName`` 指向源会话的临时文件，接收方只能看到「[XML消息]」。

        这里把摘要取出来重建成一层文本记录：图片会退化成 ``[图片]`` 字样，
        但至少接收方点得开、看得见文字，而不是一张死卡。
        """
        titles = titles_of_multimsg(segment)
        if not titles:
            result.errors.append("多消息卡片没有摘要，原样保留")
            return [segment]
        if not self.capabilities.relay_accepts_nested_node:
            # 后端不认嵌套 node，原样保留（它自己能保真就保真）
            return [segment]

        head, *rest = titles
        body = rest or titles
        result.degraded.append(head)
        result.errors.append(
            f"内层聊天记录「{head}」拿不到原始内容，已降级为 {len(body)} 条摘要文本"
        )
        return [
            build_node(
                [{"type": "text", "data": {"text": title}}],
                uin="0",
                name="聊天记录",
                time=0,
            )
            for title in body
        ]

    def _fallback_segments(self, forward_id: str, result: RelayContent) -> list[dict]:
        """实在展开不了：能留 id 就留，否则丢弃。"""
        if forward_id:
            result.unresolved.append(forward_id)
            if self.capabilities.relay_accepts_forward_segment:
                return [{"type": "forward", "data": {"id": forward_id}}]
        result.errors.append(f"内层转发 {forward_id[:16] or '<无 id>'} 无法展开")
        return []

    async def _fetch(self, forward_id: str, result: RelayContent, *, level: int) -> list[dict]:
        api_result = await self.kernel.call("get_forward_msg", {"message_id": forward_id})
        if not api_result.ok:
            result.errors.append(f"get_forward_msg({forward_id[:16]}…) 失败：{api_result.error}")
            result.unresolved.append(forward_id)
            return []
        result.expanded.append(forward_id)
        return api_result.messages
