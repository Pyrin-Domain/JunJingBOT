"""跨轮对话留存：第一轮的完整上下文 + 之后每轮的增量。

为什么不每轮重新渲染 ``ChatContext``
------------------------------------
每轮渲染出来的字符串里都有「现在=HH:MM」和不断变长的 ``<history>``，
prompt 前缀每轮都不同，服务端的前缀缓存永远命中不了；同一批历史还会在
上下文里反复出现好几遍。

这里把**第一轮真正发出去的那段文本冻结下来**，之后每轮只追加
``<current>``：

    第 1 轮: H1(完整 context) → A1
    第 2 轮: H1, A1, H2(增量)          ← 前缀 = H1，和上一轮逐字节相同
    第 3 轮: H1, A1, H2, A2, H3

为什么超额就「整段清空」而不是滑窗
----------------------------------
滑动窗口（只留最近 N 轮）每轮都在改前缀，缓存照样命中不了，模型还会看到
「缺失的中间轮」。所以超过 ``max_turns`` / ``max_chars`` 时整段丢掉，下一轮
重新发一次完整上下文 —— 断一次，换来前面 8 轮的稳定复用。

为什么续说（``<next>``）要清空
------------------------------
续说那一轮实际发生的是 ``H → A → H(继续) → A``，而这里只存得下一对
``(H, A)``。回放序列和真实序列对不上，前缀已经不成立了，清掉重来更干净。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DEFAULT_MAX_CHARS", "DEFAULT_MAX_TURNS", "DEFAULT_TTL", "ThreadTurns", "Turn"]

#: 多久没说话就作废（秒）。群里隔半小时基本是新话题了。
DEFAULT_TTL = 30 * 60
#: 最多留几轮。超过就整段清空、下一轮重发完整上下文。
DEFAULT_MAX_TURNS = 8
#: 留存文本的字符预算（每轮完整回复 + 增量，中文约 1 字 = 1 token）。
DEFAULT_MAX_CHARS = 12000


@dataclass
class Turn:
    """一轮对话：当轮实际发出去的 Human 文本 + 模型原始输出。"""

    human: str
    ai: str
    at: float = field(default_factory=time.time)
    #: 本轮唯一的身份（一般用当前消息的 message_id）。并发下两条消息同时在跑
    #: 时，用它去重；**不要**用 human 文本去重 —— 连发两句「好」会被误判成重复。
    token: str = ""


class ThreadTurns:
    """按会话保存「第一轮 + 之后每轮」，给多轮 messages 拼装用。"""

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.ttl = float(ttl)
        self.max_turns = max(1, int(max_turns))
        self.max_chars = max(1000, int(max_chars))
        self._turns: dict[str, list[Turn]] = {}

    # ── 读 ──
    def get(self, thread_id: str) -> list[Turn]:
        """当前可复用的轮次（过期就清掉，返回空表示该走完整上下文）。"""
        key = str(thread_id or "")
        if not key:
            return []
        turns = self._turns.get(key) or []
        if not turns:
            return []
        if time.time() - turns[-1].at > self.ttl:
            self._turns.pop(key, None)
            return []
        return list(turns)

    def started(self, thread_id: str) -> bool:
        return bool(self.get(thread_id))

    # ── 写 ──
    def start(self, thread_id: str, human: str, ai: str, *, token: str = "") -> None:
        """这一轮发的是完整上下文：作为新一段的起点。"""
        key = str(thread_id or "")
        if not key:
            return
        self._turns[key] = [
            Turn(human=str(human or ""), ai=str(ai or ""), token=str(token or ""))
        ]

    def append(
        self,
        thread_id: str,
        human: str,
        ai: str,
        *,
        token: str = "",
    ) -> bool:
        """追加本轮增量；返回 False 表示已清空，下一轮要走完整上下文。"""
        key = str(thread_id or "")
        if not key:
            return False
        turns = self.get(key)
        if not turns:
            return False

        token = str(token or "")
        # 并发下同一轮可能被追加两次（同一条消息重复投递）。只按 token 判重：
        # 拿 human 文本判重会把「连发两句一样的话」当成重复。
        if token and any(turn.token == token for turn in turns):
            return True

        turns.append(Turn(human=str(human or ""), ai=str(ai or ""), token=token))
        if len(turns) > self.max_turns or self.size(turns) > self.max_chars:
            self._turns.pop(key, None)
            return False
        self._turns[key] = turns
        return True

    def reset(self, thread_id: str) -> None:
        self._turns.pop(str(thread_id or ""), None)

    def clear(self) -> None:
        self._turns.clear()

    # ── 统计 ──
    @staticmethod
    def size(turns: list[Turn]) -> int:
        return sum(len(turn.human) + len(turn.ai) for turn in turns)

    def stats(self, thread_id: str) -> dict[str, Any]:
        turns = self.get(thread_id)
        return {
            "turns": len(turns),
            "chars": self.size(turns) if turns else 0,
            "started": bool(turns),
        }
