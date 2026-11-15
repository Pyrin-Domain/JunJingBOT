"""跨轮留存：把「这一轮翻出来的大块内容」暂存在会话里，下一轮还能取回。

为什么需要
----------
``QQBotAgent.chat()`` 是**无状态**的：每轮只发一条 ``HumanMessage``，历史靠
``ChatContext`` 重新渲染。工具结果只活在当轮 agent 的内部消息里，轮次一结束
就没了。

现场（2026-11-13 23:08，群 583261953）：模型调 ``digest_user`` 翻了 1500 条，
回了一句「主人要不要我把他常说的几句挑出来给你看」；主人下一句说「挑」，
这时候工具结果已经不在上下文里 —— 模型要么重新翻 1500 条（30 秒），
要么凭印象编。

这里只做两件事
--------------
1. ``put()``：工具把结果交进来，换一个 ``result_id``（按会话分桶、带 TTL、
   条数上限，全是内存操作，不发包不落盘）；
2. ``render_hint()``：每轮往提示里塞**两三行索引**，告诉模型「上一轮那些内容
   还留着，编号是多少，要细节就 ``recall_result``」。

**要不要真的取回，由模型自己决定**：不自动把 6000 字灌进每一轮，只给索引。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DEFAULT_TTL", "MAX_ENTRIES", "ResultVault", "StoredResult", "thread_key"]

#: 留存多久（秒）。够主人隔几分钟回来问「刚才那个再说说」，又不至于跨天还占着。
DEFAULT_TTL = 30 * 60
#: 每个会话最多留几条（只留最近几次，旧的自动淘汰）。
MAX_ENTRIES = 4


def thread_key(context: Any) -> str:
    """会话键，与 ``MessageProcessor.thread_id_of`` 保持一致（``group_x`` / ``private_x``）。"""
    if context is None:
        return ""
    group_id = str(getattr(context, "group_id", "") or "")
    if group_id:
        return f"group_{group_id}"
    current = getattr(context, "current", None)
    sender = getattr(current, "sender", None)
    qq = str(getattr(sender, "qq", "") or "")
    if qq:
        return f"private_{qq}"
    return ""


@dataclass
class StoredResult:
    """一条被暂存的工具结果。"""

    result_id: str
    thread_id: str
    tool: str
    text: str
    created: float
    label: str = ""
    args: dict = field(default_factory=dict)

    def age_text(self, now: float | None = None) -> str:
        seconds = max(0, int((now or time.time()) - self.created))
        if seconds < 20:
            return "刚刚"
        if seconds < 60:
            return f"{seconds} 秒前"
        if seconds < 3600:
            return f"{seconds // 60} 分钟前"
        return f"{seconds // 3600} 小时前"

    def describe(self, now: float | None = None) -> str:
        name = self.label or self.tool or "工具结果"
        return f"{self.result_id} {name} · {self.age_text(now)} · 约 {len(self.text)} 字"


class ResultVault:
    """按会话分桶的工具结果暂存区（内存，进程重启即清空）。"""

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.ttl = float(ttl)
        self.max_entries = max(1, int(max_entries))
        self._buckets: dict[str, list[StoredResult]] = {}
        self._counter: dict[str, int] = {}

    # ── 写 ──
    def put(
        self,
        thread_id: str,
        tool: str,
        text: str,
        *,
        args: dict | None = None,
        label: str = "",
    ) -> StoredResult | None:
        """存一段结果，返回带编号的条目；会话或内容为空时返回 None。"""
        thread_id = str(thread_id or "").strip()
        text = str(text or "")
        if not thread_id or not text.strip():
            return None

        bucket = self._live(thread_id)
        index = int(self._counter.get(thread_id, 0)) + 1
        self._counter[thread_id] = index
        entry = StoredResult(
            result_id=f"R{index}",
            thread_id=thread_id,
            tool=str(tool or ""),
            text=text,
            created=time.time(),
            label=str(label or ""),
            args=dict(args or {}),
        )
        bucket.append(entry)
        self._buckets[thread_id] = bucket[-self.max_entries :]
        return entry

    # ── 读 ──
    def get(self, thread_id: str, result_id: str = "") -> StoredResult | None:
        """取一条；``result_id`` 为空表示「最近一条」。找不到返回 None。"""
        bucket = self._live(str(thread_id or ""))
        if not bucket:
            return None
        key = str(result_id or "").strip()
        if not key:
            return bucket[-1]
        for entry in reversed(bucket):
            if entry.result_id == key:
                return entry
        return None

    def latest(self, thread_id: str) -> StoredResult | None:
        return self.get(thread_id, "")

    def entries(self, thread_id: str) -> list[StoredResult]:
        return list(self._live(str(thread_id or "")))

    def render_hint(self, thread_id: str, *, recent: int = 2) -> str:
        """给模型的索引块；没有可复用结果时返回空串（不占 token）。

        只给「编号 + 什么内容 + 多久之前」，正文留在 vault 里 —— 让模型自己
        决定要不要 ``recall_result``，避免每轮都把几千字硬灌进上下文。
        """
        bucket = self._live(str(thread_id or ""))
        if not bucket:
            return ""
        now = time.time()
        lines = ["[可复用结果] 之前的工具结果还留着（本轮不用重新翻）："]
        for entry in bucket[-max(1, int(recent)) :]:
            lines.append(f"- {entry.describe(now)}")
        lines.append(
            '对方要是让你「挑几句 / 展开 / 具体说说 / 继续 / 给我看看」，'
            '直接调 recall_result(result_id="<编号>") 把原文取回再回答；'
            "用不上就忽略这段。"
        )
        return "\n".join(lines)

    # ── 维护 ──
    def clear(self, thread_id: str | None = None) -> None:
        if thread_id is None:
            self._buckets.clear()
            self._counter.clear()
            return
        key = str(thread_id or "")
        self._buckets.pop(key, None)
        self._counter.pop(key, None)

    def _live(self, thread_id: str) -> list[StoredResult]:
        """清掉过期的，返回仍有效的（就地更新桶）。"""
        if not thread_id:
            return []
        bucket = self._buckets.get(thread_id) or []
        if not bucket:
            return []
        now = time.time()
        kept = [entry for entry in bucket if now - entry.created <= self.ttl]
        if kept:
            self._buckets[thread_id] = kept
        else:
            self._buckets.pop(thread_id, None)
        return kept
