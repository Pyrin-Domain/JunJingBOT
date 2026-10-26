"""归一化的合并转发展开。

实测差异（这才是需要抽象的地方）::

    NapCat
        get_forward_msg(顶层 id)  → 5 条，其中嵌套的那条直接带 data.content（34 条）
        get_forward_msg(内层 id)  → retcode=200「消息已过期或者为内层消息，无法获取转发消息」

    SnowLuma
        get_forward_msg(顶层 id)  → 5 条，嵌套那条只有 data.id，没有 content
        get_forward_msg(内层 id)  → 正常返回 34 条（一次只展开一层）

所以：

- NapCat **只能**靠内联 ``content`` 递归，绝不能拿内层 id 再发包（会失败）；
- SnowLuma **必须**靠内层 id 再发包，因为没有内联。

统一算法（一份代码同时吃两种后端）::

    遇到 forward 段
      ├─ 有 data.content  → 就地递归（NapCat 走这条，不发包）
      ├─ 没有 content 且后端支持内层 id → 发包展开（SnowLuma 走这条）
      └─ 两者都不行 → 留一个占位节点，不丢父消息、不死循环

再用 ``max_depth`` / ``max_items`` / ``visited`` 三重保险挡递归转发和爆炸。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .kernal import OneBotKernel

__all__ = [
    "ForwardEntry",
    "ForwardExpansion",
    "ForwardExpander",
    "extract_forward_id",
    "forward_count_from_card",
]


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════
@dataclass
class ForwardEntry:
    """展开后的一条**叶子**消息（本身不再是转发）。"""

    message: dict
    #: 在第几层（顶层子消息 = 1）。
    depth: int = 1
    #: 这一条是从哪儿来的：``fetch``=发包拿到，``inline``=内联 content。
    via: str = "fetch"


@dataclass
class ForwardExpansion:
    root_id: str = ""
    entries: list[ForwardEntry] = field(default_factory=list)
    #: 卡片上写的总条数（拿不到为 0）。
    expected: int = 0
    #: 展开层数。
    depth_reached: int = 0
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    #: 拿不到内层又没法发包时，记下占位的 forward id。
    unresolved: list[str] = field(default_factory=list)

    @property
    def messages(self) -> list[dict]:
        return [entry.message for entry in self.entries]

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not self.errors

    def describe(self) -> str:
        head = f"根={self.root_id[:16]}… 叶子={len(self.entries)} 层={self.depth_reached}"
        if self.expected and len(self.entries) < self.expected:
            head += f" 卡片称={self.expected}"
        if self.errors:
            head += f" 错误={self.errors[0]!r}"
        return head


# ══════════════════════════════════════════════════════════
# 从事件里找 forward id
# ══════════════════════════════════════════════════════════
_JSON_CARD_APPS = ("com.tencent.multimsg",)
_CQ_FORWARD = re.compile(r"\[CQ:forward,id=([^\],]+)")


def extract_forward_id(event: Any) -> str | None:
    """从一条消息事件里取出能喂给 ``get_forward_msg`` 的 id。

    三种形态都认：

    1. 老式段：``message`` 里有 ``{"type": "forward", "data": {"id": ...}}``
    2. 新式 JSON 卡片：``com.tencent.multimsg`` 的 ``meta.detail.resid``
    3. 兜底：``raw_message`` 里的 ``[CQ:forward,id=...]``

    注意 SnowLuma 的 id 是长 base64 串（含 ``+ / =``），必须原样保留，
    截断或 URL 编码过都会拿到 "download forward message payload is empty"。
    """
    if not isinstance(event, dict):
        return None

    for seg in event.get("message") or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "forward":
            data = seg.get("data") or {}
            value = data.get("id") or data.get("resid")
            if value:
                return str(value)

    for seg in event.get("message") or []:
        if not isinstance(seg, dict) or seg.get("type") != "json":
            continue
        payload = _loads((seg.get("data") or {}).get("data"))
        if not isinstance(payload, dict):
            continue
        if payload.get("app") not in _JSON_CARD_APPS:
            continue
        detail = ((payload.get("meta") or {}).get("detail")) or {}
        if isinstance(detail, dict) and detail.get("resid"):
            return str(detail["resid"])

    match = _CQ_FORWARD.search(str(event.get("raw_message") or ""))
    if match:
        return match.group(1)
    return None


def forward_count_from_card(event: Any) -> int:
    """卡片文案里的「查看N条转发消息」；拿不到返回 0。"""
    try:
        text = json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    match = re.search(r"查看(\d+)条转发消息", text) or re.search(r"(\d+)条转发", text)
    return int(match.group(1)) if match else 0


def _loads(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _segments(message: dict) -> list[dict]:
    """取消息段；部分后端把子消息放在 ``content`` 里。"""
    segs = message.get("message")
    if not segs:
        segs = message.get("content")
    return [s for s in (segs or []) if isinstance(s, dict)]


# ══════════════════════════════════════════════════════════
# 展开器
# ══════════════════════════════════════════════════════════
class ForwardExpander:
    """把一个转发 id 摊平成叶子消息列表。"""

    def __init__(
        self,
        kernel: OneBotKernel,
        *,
        max_depth: int = 3,
        max_items: int = 200,
    ) -> None:
        self.kernel = kernel
        self.max_depth = max_depth
        self.max_items = max_items

    @property
    def capabilities(self):
        return self.kernel.capabilities

    async def expand(self, forward_id: Any, *, expected: int = 0) -> ForwardExpansion:
        expansion = ForwardExpansion(root_id=str(forward_id or ""), expected=int(expected or 0))
        if not forward_id:
            expansion.errors.append("没有 forward id")
            return expansion
        await self._collect(str(forward_id), level=1, via="fetch", expansion=expansion, visited=set())
        expansion.truncated = expansion.truncated or (
            bool(expansion.expected) and len(expansion.entries) < expansion.expected
        )
        return expansion

    async def expand_event(self, event: Any) -> ForwardExpansion:
        """从消息事件直接展开；不是转发就返回空结果。"""
        fid = extract_forward_id(event)
        if not fid:
            return ForwardExpansion(errors=["不是转发消息"])
        return await self.expand(fid, expected=forward_count_from_card(event))

    # ── 内部 ──
    async def _collect(
        self,
        forward_id: str,
        *,
        level: int,
        via: str,
        expansion: ForwardExpansion,
        visited: set[str],
    ) -> None:
        """拉一个转发，并把它的子消息按 ``level`` 层摊平。"""
        if level > self.max_depth:
            expansion.truncated = True
            expansion.errors.append(
                f"超过 max_depth={self.max_depth}，{forward_id[:16]}… 未展开"
            )
            expansion.unresolved.append(forward_id)
            return
        if forward_id in visited:
            expansion.errors.append(f"转发递归：{forward_id[:16]}… 已展开过")
            expansion.truncated = True
            return
        visited.add(forward_id)

        result = await self.kernel.call("get_forward_msg", {"message_id": forward_id})
        if not result.ok:
            expansion.errors.append(f"get_forward_msg({forward_id[:16]}…) 失败：{result.error}")
            expansion.unresolved.append(forward_id)
            return

        expansion.depth_reached = max(expansion.depth_reached, level)
        await self._walk_items(result.messages, level=level, via=via, expansion=expansion, visited=visited)

    async def _walk_items(
        self,
        items: list[dict],
        *,
        level: int,
        via: str,
        expansion: ForwardExpansion,
        visited: set[str],
    ) -> None:
        """摊平一层子消息；遇到嵌套转发就往下走。"""
        for item in items:
            if len(expansion.entries) >= self.max_items:
                expansion.truncated = True
                return
            if not isinstance(item, dict):
                continue
            nested = [s for s in _segments(item) if s.get("type") == "forward"]
            if not nested:
                expansion.entries.append(ForwardEntry(message=item, depth=level, via=via))
                continue
            for seg in nested:
                await self._descend(seg, level=level + 1, expansion=expansion, visited=visited)

    async def _descend(
        self,
        seg: dict,
        *,
        level: int,
        expansion: ForwardExpansion,
        visited: set[str],
    ) -> None:
        """处理一个嵌套 forward 段；``level`` 是它子消息的层级。"""
        if level > self.max_depth:
            expansion.truncated = True
            expansion.errors.append(f"嵌套转发超过 max_depth={self.max_depth}（第 {level} 层）")
            return

        data = seg.get("data") or {}
        inline = data.get("content")
        inner_id = str(data.get("id") or data.get("resid") or "")

        # ① 内联优先：NapCat 走这条，而且它**不能**用内层 id 发包。
        if isinstance(inline, list) and inline:
            expansion.depth_reached = max(expansion.depth_reached, level)
            await self._walk_items(inline, level=level, via="inline", expansion=expansion, visited=visited)
            return

        # ② 没内联才发包：SnowLuma 走这条。
        if inner_id and self.capabilities.forward_inner_id_fetchable:
            await self._collect(
                inner_id,
                level=level,
                via="fetch",
                expansion=expansion,
                visited=visited,
            )
            return

        # ③ 既没内联也发不了包：留占位，不丢父消息也不死循环。
        expansion.unresolved.append(inner_id or "<无 id>")
        expansion.errors.append(
            f"第 {level} 层转发既无内联 content 也不能用内层 id 展开"
            f"（id={inner_id[:24] or '空'}）"
        )
