"""跨后端统一的消息游标。

为什么需要它
------------

同一条消息在两个后端里有三套编号：

======================  ==========================  ==========================
字段                     NapCat                      SnowLuma
======================  ==========================  ==========================
``message_id``          int32 哈希                   int32 哈希（**值不同**）
``message_seq``         同一个哈希                   **全局序号**（184/185…）
``real_seq``            全局序号 "184"（str）        **没有这个字段**
======================  ==========================  ==========================

所以「全局序号」是唯一能跨后端对齐的身份：

- :meth:`MessageCursor.from_message` 自动从 ``real_seq`` 或「不等于 message_id 的
  message_seq」里把它抠出来；
- :meth:`MessageCursor.matches` 优先比全局序号，其次比 ``message_id``（同后端时
  可靠），最后才退回 ``message_seq``；
- :meth:`MessageCursor.native_value` 取某个后端真正能用的游标值，给
  :class:`~AbstractionLayer.history.HistoryPager` 拼参数用。

**不要**用 ``anchor_of`` 那套「随便哪个字段相等就算同一条」的做法做断点比对：
NapCat 的 ``message_seq`` 是哈希、SnowLuma 的是 184 这种小序号，两边混用会
把不同的消息判成同一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["MessageCursor", "as_int", "same_value"]


def as_int(value: Any) -> int | None:
    """尽量把 ``"184"`` / ``184`` / ``184.0`` 转成 ``int``；失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def same_value(left: Any, right: Any) -> bool:
    """宽松相等：数字与数字字符串视为相等。"""
    if left is None or right is None:
        return False
    a, b = as_int(left), as_int(right)
    if a is not None and b is not None:
        return a == b
    return str(left).strip() == str(right).strip()


@dataclass(frozen=True)
class MessageCursor:
    """一条消息的游标身份。

    :param message_id: 后端本地哈希；跨后端**不可比**。
    :param message_seq: 原始 ``message_seq``；含义随后端变化，不要直接用。
    :param real_seq: 归一化后的全局序号；跨后端可比，这是唯一可靠的跨后端身份。
    :param time: 秒级时间戳，兜底用。
    """

    message_id: Any = None
    message_seq: Any = None
    real_seq: int | None = None
    time: int | None = None

    # ── 构造 ──
    @classmethod
    def from_message(cls, message: Any) -> "MessageCursor":
        if isinstance(message, MessageCursor):
            return message
        if not isinstance(message, dict):
            return cls()

        message_id = message.get("message_id")
        if message_id is None:
            message_id = message.get("real_id")
        message_seq = message.get("message_seq")

        # 全局序号：NapCat 在 real_seq，SnowLuma 在 message_seq。
        real_seq = as_int(message.get("real_seq"))
        if real_seq is None and message_seq is not None and not same_value(message_seq, message_id):
            real_seq = as_int(message_seq)

        return cls(
            message_id=message_id,
            message_seq=message_seq,
            real_seq=real_seq,
            time=as_int(message.get("time")),
        )

    # ── 查询 ──
    @classmethod
    def parse(cls, text: Any) -> "MessageCursor":
        """解析命令行 / 配置文件里的游标写法。

        ==================  ========================================
        写法                 含义
        ==================  ========================================
        ``seq:184``          全局序号 184（跨后端通用，只能用于**比对**）
        ``id:501493501``     message_id（同后端可翻页）
        ``raw:184``          原始 message_seq（含义随后端变化）
        ``184``              只当全局序号（同 ``seq:``）
        ==================  ========================================

        注意：全局序号**不能**直接当翻页游标——实测两个后端都会忽略
        ``real_seq`` 参数并返回最新一页。它能做的是「找到同一条消息」，
        要翻页必须先定位到那条消息、拿到它自己的 ``message_seq``/``message_id``。
        """
        if isinstance(text, MessageCursor):
            return text
        if isinstance(text, dict):
            return cls.from_message(text)
        raw = str(text or "").strip()
        if not raw:
            return cls()
        head, sep, tail = raw.partition(":")
        key = head.lower() if sep else ""
        if key in {"seq", "real_seq", "real"}:
            return cls(real_seq=as_int(tail))
        if key in {"id", "mid", "message_id"}:
            value = as_int(tail)
            return cls(message_id=value if value is not None else tail)
        if key in {"raw", "message_seq"}:
            value = as_int(tail)
            return cls(message_seq=value if value is not None else tail)
        return cls(real_seq=as_int(raw))

    @property
    def has_identity(self) -> bool:
        return any(
            value is not None for value in (self.real_seq, self.message_id, self.message_seq)
        )

    @property
    def is_global_seq_known(self) -> bool:
        return self.real_seq is not None

    def value(self, field: str) -> Any:
        return getattr(self, field, None)

    def native_value(self, field: str) -> Any:
        """某个后端指定的游标字段能不能用这个游标取到值。

        ``field`` 是 ``BackendCapabilities.history_cursor``。取不到就返回 None，
        调用方**不应**改发别的字段——后端会静默忽略并返回居中窗口。
        """
        if field == "message_seq":
            # NapCat 的 message_seq 是哈希，和 message_id 同值；SnowLuma 的是全局序号。
            if self.message_seq is not None:
                return self.message_seq
            return None
        if field == "message_id":
            return self.message_id
        if field == "real_seq":
            return self.real_seq
        return None

    # ── 比较 ──
    def matches(self, other: Any) -> bool:
        """是否指向同一条消息。

        顺序很关键：全局序号优先（跨后端唯一可靠），其次 ``message_id``
        （同后端唯一），最后 ``message_seq``。
        """
        other = self.from_message(other) if not isinstance(other, MessageCursor) else other
        if self.real_seq is not None and other.real_seq is not None:
            return self.real_seq == other.real_seq
        if self.message_id is not None and other.message_id is not None:
            return same_value(self.message_id, other.message_id)
        if self.message_seq is not None and other.message_seq is not None:
            return same_value(self.message_seq, other.message_seq)
        if self.time is not None and other.time is not None:
            return self.time == other.time
        return False

    def sort_key(self) -> tuple[int, int, int]:
        if self.real_seq is not None:
            return (0, self.real_seq, 0)
        return (1, self.time or 0, as_int(self.message_id) or 0)

    def describe(self) -> str:
        parts = []
        if self.real_seq is not None:
            parts.append(f"seq={self.real_seq}")
        if self.message_id is not None:
            parts.append(f"id={self.message_id}")
        if self.time is not None:
            parts.append(f"t={self.time}")
        return " ".join(parts) or "<空游标>"

    def __bool__(self) -> bool:
        return self.has_identity
