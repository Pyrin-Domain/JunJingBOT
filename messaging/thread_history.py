"""
ThreadHistory —— 临时的会话历史缓存（按群 / 私聊一份，可销毁）

为什么要它：
    一次只爬十几条消息，遇到「Bot 插话 / 回完 @ 之后对方接着回你」就不够用了——
    真正的上下文要从 Bot 开口那一句算起，甚至更早。这里按会话维护一小段滚动历史，
    爬历史时把它并进去，就不会因为窗口太短把话头截断。

为什么是临时的：
    - 一段时间（`ttl_seconds`，默认 2 小时）没有新消息就自动销毁
    - 会话数超过 `max_threads` 时，先丢最久没动静的那份
    - 也可以手动 `drop(thread_id)` / `clear()`
    - 只在内存里，不落盘；Bot 一重启就全没了（断点那套是另一回事）

两个用途：
    1. `merge_history()`：把缓存里更早的那段补到刚爬到的历史前面
    2. **敏感期**：Bot 说过话之后的一小段时间里（`mark_bot_spoke` → `is_hot`），
       对方回它、或者群里继续聊，它会更容易接话
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

DEFAULT_MAX_MESSAGES = 80      # 单个会话最多留多少条
DEFAULT_TTL_SECONDS = 2 * 60 * 60   # 2 小时没动静就销毁
DEFAULT_HOT_SECONDS = 10 * 60       # Bot 说完话后的「敏感期」
DEFAULT_MAX_THREADS = 64            # 最多同时缓存多少个会话


@dataclass
class ThreadHistory:
    """一个会话的临时历史。"""

    thread_id: str
    max_messages: int = DEFAULT_MAX_MESSAGES
    messages: list = field(default_factory=list)
    last_active: float = 0.0        # time.monotonic()
    bot_spoke_at: float = 0.0       # 最近一次 Bot 在这个会话发言（monotonic）

    # -------- 写入 --------
    def upsert(self, message) -> None:
        """同 id 覆盖（保留信息更全的那条），否则追加；超长从最旧开始丢。"""
        mid = str(getattr(message, "message_id", "") or "")
        if mid:
            for index, old in enumerate(self.messages):
                if str(getattr(old, "message_id", "") or "") == mid:
                    if _is_richer(message, old):
                        self.messages[index] = message
                    return
        self.messages.append(message)
        overflow = len(self.messages) - self.max_messages
        if overflow > 0:
            del self.messages[:overflow]

    def mark_bot_spoke(self, now: Optional[float] = None) -> None:
        self.bot_spoke_at = time.monotonic() if now is None else now

    def touch(self, now: Optional[float] = None) -> None:
        self.last_active = time.monotonic() if now is None else now

    # -------- 读取 --------
    def is_hot(
        self,
        now: Optional[float] = None,
        hot_seconds: float = DEFAULT_HOT_SECONDS,
    ) -> bool:
        """Bot 刚说过话的一小段时间 = 敏感期。"""
        if not self.bot_spoke_at:
            return False
        moment = time.monotonic() if now is None else now
        return 0 <= moment - self.bot_spoke_at <= hot_seconds

    def find(self, message_id):
        """按 message_id 找缓存里的那条（用于补引用摘要 / 判断是不是 Bot 说的）。"""
        target = str(message_id or "")
        if not target:
            return None
        for message in self.messages:
            if str(getattr(message, "message_id", "") or "") == target:
                return message
        return None

    def is_bot_message(self, message_id) -> bool:
        found = self.find(message_id)
        return bool(found is not None and getattr(getattr(found, "sender", None), "is_self", False))

    def last_bot_message(self):
        for message in reversed(self.messages):
            if getattr(getattr(message, "sender", None), "is_self", False):
                return message
        return None

    def snapshot(self, limit: Optional[int] = None) -> list:
        if limit is None or limit <= 0:
            return list(self.messages)
        return list(self.messages[-limit:])

    def expired(self, now: Optional[float] = None, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> bool:
        moment = time.monotonic() if now is None else now
        return moment - self.last_active > ttl_seconds

    def stats(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "messages": len(self.messages),
            "hot": self.is_hot(),
        }


def _is_richer(new, old) -> bool:
    """新的一条是不是信息更全（有引用摘要 / 展开过转发 / 段更多）。"""
    if getattr(new, "quote", None) and not getattr(old, "quote", None):
        return True
    if getattr(new, "forward", None) and not getattr(old, "forward", None):
        return True
    return len(getattr(new, "segments", []) or []) > len(getattr(old, "segments", []) or [])


def merge_history(buffered: Iterable, fetched: Iterable) -> list:
    """把缓存里的历史并到刚爬到的历史里（按 message_id 去重，按时间排）。

    - 同一条消息以 fetched 为准（它是带引用摘要的完整版本）
    - 缓存可能补上「比窗口更早」的一段，也可能有「刚发出去、历史接口还没返回」的一条
    - 全都有时间时按时间稳定排序，避免把并发进来的消息插错位置
    """
    fetched = list(fetched or [])
    buffered = list(buffered or [])
    if not buffered:
        return fetched

    seen = {str(getattr(m, "message_id", "") or "") for m in fetched}
    extra = [
        m
        for m in buffered
        if str(getattr(m, "message_id", "") or "")
        and str(getattr(m, "message_id", "") or "") not in seen
    ]
    if not extra:
        return fetched

    merged = extra + fetched
    if all(getattr(m, "time", None) for m in merged):
        merged = sorted(merged, key=lambda m: m.time)
    return merged


def make_bot_message(
    text: str,
    *,
    self_id: str = "",
    name: str = "",
    message_id="",
    now: Optional[datetime] = None,
):
    """把 Bot 自己发出去的一句话包装成 ChatMessage（用于敏感期 / 后续续聊）。"""
    from .models import ChatMessage, Segment, Sender

    return ChatMessage(
        message_id=str(message_id or ""),
        time=now or datetime.now(),
        sender=Sender(qq=str(self_id or ""), name=str(name or ""), is_self=True),
        segments=[Segment("text", text=str(text or ""))],
    )


class ThreadHistoryStore:
    """会话 → ThreadHistory 的容器，负责 TTL / 数量上限 / 手动销毁。"""

    def __init__(
        self,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        hot_seconds: float = DEFAULT_HOT_SECONDS,
        max_threads: int = DEFAULT_MAX_THREADS,
    ):
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds
        self.hot_seconds = hot_seconds
        self.max_threads = max_threads
        self._threads: dict[str, ThreadHistory] = {}
        self._last_sweep = 0.0

    # -------- 生命周期 --------
    def get(self, thread_id: str, *, create: bool = False) -> Optional[ThreadHistory]:
        key = str(thread_id or "")
        if not key:
            return None
        history = self._threads.get(key)
        if history is None and create:
            history = ThreadHistory(thread_id=key, max_messages=self.max_messages)
            history.touch()
            self._threads[key] = history
            self._evict_if_needed()
        return history

    def drop(self, thread_id: str) -> bool:
        """手动销毁某个会话的缓存（用户说的「可以被销毁」）。"""
        return self._threads.pop(str(thread_id or ""), None) is not None

    def clear(self) -> None:
        self._threads.clear()

    def sweep(self, now: Optional[float] = None) -> int:
        """清掉过期的会话；返回销毁了几个。"""
        moment = time.monotonic() if now is None else now
        self._last_sweep = moment
        dead = [key for key, item in self._threads.items() if item.expired(moment, self.ttl_seconds)]
        for key in dead:
            self._threads.pop(key, None)
        return len(dead)

    def _evict_if_needed(self) -> None:
        overflow = len(self._threads) - self.max_threads
        if overflow <= 0:
            return
        # 最久没动静的先丢
        ordered = sorted(self._threads.values(), key=lambda item: item.last_active)
        for item in ordered[:overflow]:
            self._threads.pop(item.thread_id, None)

    # -------- 写入 / 读取 --------
    def remember(self, thread_id: str, message, *, now: Optional[float] = None) -> Optional[ThreadHistory]:
        history = self.get(thread_id, create=True)
        if history is None:
            return None
        history.upsert(message)
        history.touch(now)
        moment = time.monotonic() if now is None else now
        if moment - self._last_sweep > 60:   # 一分钟扫一次就够了
            self.sweep(moment)
        return history

    def mark_bot_spoke(self, thread_id: str, now: Optional[float] = None) -> None:
        history = self.get(thread_id, create=True)
        if history is not None:
            history.mark_bot_spoke(now)
            history.touch(now)

    def snapshot(self, thread_id: str, limit: Optional[int] = None) -> list:
        history = self.get(thread_id)
        return history.snapshot(limit) if history else []

    def is_hot(self, thread_id: str, *, hot_seconds: Optional[float] = None) -> bool:
        history = self.get(thread_id)
        if history is None:
            return False
        return history.is_hot(hot_seconds=self.hot_seconds if hot_seconds is None else hot_seconds)

    def is_bot_message(self, thread_id: str, message_id) -> bool:
        history = self.get(thread_id)
        return bool(history and history.is_bot_message(message_id))

    def find(self, thread_id: str, message_id):
        history = self.get(thread_id)
        return history.find(message_id) if history else None

    def stats(self) -> list[dict]:
        return [item.stats() for item in self._threads.values()]
