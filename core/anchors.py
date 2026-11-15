"""消息游标（history anchor）的通用表示。

同一个消息在不同 OneBot 后端里有不同的“可翻页 ID”：

- NapCat：``message_seq`` 是历史接口的游标；
- SnowLuma：``message_id``（有符号 int32，可能为负）才是游标；
- 两者的事件里通常同时带 ``message_id`` / ``message_seq`` / ``real_id``。

业务层不判断后端，只把 :class:`MessageAnchor` 里的两个值一起交给
``transport.api``，由后端档案选择真正发出去的那个字段。

**范围说明（重要）**：本模块的比较是「任意字段相等就算同一条」的**宽松**语义，
专门服务「用户给了一个 id，要把区间找回来」这类场景（``messaging.history``
的区间召回、``context_builder`` 的引用定位）——那里我们不知道用户给的到底是
``message_id`` 还是 ``message_seq``，只能都试。

**不要拿它做断点比对**：NapCat 的 ``message_seq`` 是哈希、SnowLuma 的是 184
这种小序号，两边混比会把不同消息判成同一条。断点 / 翻页的身份判断一律用
:class:`AbstractionLayer.cursor.MessageCursor`（``real_seq → real_id →
message_id → message_seq`` 依次严格比对）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ANCHOR_FIELDS = ("message_seq", "message_id", "real_id")


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _normalize_id(value: Any) -> Any:
    value = _clean(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def same_id(left: Any, right: Any) -> bool:
    """宽松比较两个消息 ID：数字/数字字符串视为相等。"""
    a = _normalize_id(left)
    b = _normalize_id(right)
    if a is None or b is None:
        return False
    return a == b


@dataclass(frozen=True)
class MessageAnchor:
    """一条消息的游标集合。"""

    message_seq: Any = None
    message_id: Any = None
    real_id: Any = None

    @classmethod
    def from_message(cls, message: Any) -> "MessageAnchor":
        if not isinstance(message, dict):
            return cls()
        message_id = _clean(message.get("message_id"))
        real_id = _clean(message.get("real_id"))
        return cls(
            message_seq=_clean(message.get("message_seq")),
            # message_id 缺失时用 real_id 兜底；real_id 缺失时反过来兜底。
            message_id=message_id if message_id is not None else real_id,
            real_id=real_id if real_id is not None else message_id,
        )

    @property
    def has_anchor(self) -> bool:
        return any(getattr(self, field) is not None for field in _ANCHOR_FIELDS)

    def value(self, field: str) -> Any:
        return getattr(self, field, None)

    def preferred(self, anchor: str | None) -> Any:
        """按后端偏好的字段取值；没有就按 seq → id → real_id 兜底。"""
        if anchor:
            value = self.value(anchor)
            if value is not None:
                return value
        for field in _ANCHOR_FIELDS:
            value = self.value(field)
            if value is not None:
                return value
        return None

    def as_kwargs(self) -> dict[str, Any]:
        """交给历史 API 的关键字参数（两个都带，后端自己挑）。"""
        return {
            "message_seq": self.message_seq,
            "message_id": self.message_id,
        }

    def matches(self, other: Any) -> bool:
        if isinstance(other, MessageAnchor):
            return self.intersects(other)
        return any(same_id(value, other) for value in self.values())

    def intersects(self, other: "MessageAnchor") -> bool:
        return any(same_id(left, right) for left in self.values() for right in other.values())

    def values(self) -> tuple[Any, ...]:
        # real_id 与 message_id 常常相同，去重后比较更干净。
        seen: list[Any] = []
        for field in _ANCHOR_FIELDS:
            value = self.value(field)
            if value is None:
                continue
            if any(same_id(value, item) for item in seen):
                continue
            seen.append(value)
        return tuple(seen)

    def __bool__(self) -> bool:
        return self.has_anchor


def anchor_of(message: Any) -> MessageAnchor:
    if isinstance(message, MessageAnchor):
        return message
    return MessageAnchor.from_message(message)


def anchor_kwargs(
    message: Any = None,
    *,
    message_seq: Any = None,
    message_id: Any = None,
) -> dict[str, Any]:
    """组合出历史 API 参数。

    显式传入的值优先；只传一个值时把它同时当作两个游标（调用方通常拿到的是
    用户输入的“消息 ID”，具体是 seq 还是 id 由后端决定）。
    """
    anchor = anchor_of(message)
    if message_seq is not None or message_id is not None:
        # 显式给了游标就不要再混入 message 里的另一个字段，否则可能把
        # “覆盖当前消息”变成“同时指向两条不同消息”。
        seq = message_seq
        mid = message_id
        if seq is None and mid is not None:
            seq = mid
        if mid is None and seq is not None:
            mid = seq
        return {"message_seq": seq, "message_id": mid}
    return anchor.as_kwargs()


def anchor_matches(message: Any, value: Any) -> bool:
    """消息的任意一个游标等于 ``value`` 即视为匹配。"""
    return anchor_of(message).matches(value)


# 曾经还有 ``anchors_equal`` / ``anchor_value``：前者是「两条消息任意游标有交集」，
# 生产代码一处都没用过（唯一的调用点在断点恢复里，已改成
# ``MessageCursor.matches`` 的严格比对），留着只会被人误用在断点上；后者只是
# ``anchor.preferred()`` 的一行包装。两者都已删除。


__all__ = [
    "MessageAnchor",
    "anchor_kwargs",
    "anchor_matches",
    "anchor_of",
    "same_id",
]
