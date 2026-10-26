"""群聊的「局面」和「我自己的位置」——给插话判断用的两段结构化信号。

为什么不把这些丢给模型自己看：
    judge 用的是小模型，让它从十几行聊天记录里数「几个人在聊」「是不是两个人在
    私聊」「有没有人提问没人接」，经常数错；这些指标用代码算是 100% 准的，而且
    只在渲染时占一两行。模型应该只做它真正擅长的事：判断这一刻缺什么角色。

两段信号：
    <pulse> 这段对话的局面：几个人、聊了多久、安静了多久、最后一条是什么状态、
            有没有人问了话还没人接（这是插话价值最高的时刻）。
    <me>    我上一次在这里说话是什么时候、说了什么、今天说过几次。
            没有这段，模型会「刚说完又接一句」，这是显得烦的第一大来源。

本模块只做纯数据计算：不读配置、不发包、不看时间以外的东西，方便单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

# 收尾：短句里出现这些词，这段对话基本就结束了（「那我去睡了」「先下了」）
_ENDED_RE = re.compile(
    r"(晚安|睡了|去睡|睡觉|先睡|下了|下线|溜了|走了|拜拜|88|886|吃饭去|洗澡|"
    r"上课了|先这样|散了|明天见|回聊|撤了|不聊了)"
)
_ENDED_MAX_CHARS = 15

# 话题能量耗尽：接不上了
_LOW_ENERGY_RE = re.compile(r"^(哈哈+|草+|笑死|好家伙|牛逼|绝了|\.\.\.+|…+|6+|啊这|确实|是的)$")

# 提问信号（负向断言避开「没什么」「不怎么样」这类否定说法）
_QUESTION_RE = re.compile(
    r"[?？]|(?<![没不])(什么|怎么|为什么|为啥|谁|哪里|哪儿|多少|"
    r"能不能|可不可以|是不是|有没有|要不要|会不会|请问|帮我|求推荐|求个)"
)

_QUESTION_TAIL = 4      # 只回看最后几条找悬空提问


@dataclass
class Pulse:
    """一段对话现在的局面。"""

    messages: int = 0               # 窗口内别人的消息条数（不含自己）
    speakers: int = 0               # 不同的发言人个数
    span_minutes: float = 0.0       # 这段对话持续了多久
    quiet_minutes: float = 0.0      # 最后一条距现在多久
    dialogue: bool = False          # 两个人一唱一和 → 别插
    chain: bool = False             # 最后两条是同一个人自说自话
    ended: bool = False             # 最后一条像收尾
    low_energy: bool = False        # 最后一条把话题聊死了
    question_id: str = ""           # 悬空提问的消息 ID
    question_who: str = ""          # 谁问的
    question_text: str = ""         # 问的什么
    last_text: str = ""             # 最后一条正文（截断）

    @property
    def has_question(self) -> bool:
        return bool(self.question_text)

    def render(self) -> list[str]:
        """渲染成 ``<pulse>`` 里的几行；没信息时返回空列表。"""
        lines: list[str] = []
        if self.messages:
            head = f"这场对话：{self.speakers} 人说了 {self.messages} 条"
            if self.span_minutes >= 1:
                head += f"，持续 {self.span_minutes:.0f} 分钟"
            lines.append(head)
        if self.quiet_minutes >= 1:
            lines.append(f"安静：最后一条在 {self.quiet_minutes:.0f} 分钟前")
        if self.dialogue:
            lines.append("形态：两个人在来回聊，像私聊")
        if self.chain:
            lines.append("形态：最后两条是同一个人连着说")
        if self.ended:
            lines.append("状态：最后一条像在收尾")
        if self.low_energy:
            lines.append("状态：最后一条把话题聊死了")
        if self.has_question:
            who = f"{self.question_who}：" if self.question_who else ""
            lines.append(f"悬空提问：{who}「{self.question_text}」（还没人接）")
        return lines


@dataclass
class SelfPosition:
    """Bot 自己在这个会话里的位置。"""

    last_minutes: Optional[float] = None   # 上次说话距今多少分钟
    last_text: str = ""                    # 上次说了什么
    today_count: int = 0                   # 今天在这个会话说了几条

    @property
    def spoke_just_now(self) -> bool:
        return self.last_minutes is not None and self.last_minutes < 3

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.last_minutes is None:
            lines.append("我还没在这个群说过话")
        else:
            ago = (
                "刚刚"
                if self.last_minutes < 1
                else f"{self.last_minutes:.0f} 分钟前"
            )
            text = f"「{self.last_text}」" if self.last_text else ""
            lines.append(f"我上次说话：{ago}{text}")
        if self.today_count:
            lines.append(f"今天我在这里说过 {self.today_count} 次")
        if self.spoke_just_now:
            lines.append("注意：我刚说完没多久，没有新内容就别再添一句")
        return lines


# ══════════════════════════════════════════════════════════
# 计算
# ══════════════════════════════════════════════════════════
def _speaker_of(message) -> str:
    sender = getattr(message, "sender", None)
    return str(getattr(sender, "qq", "") or "")


def _is_self(message) -> bool:
    sender = getattr(message, "sender", None)
    return bool(getattr(sender, "is_self", False))


def _name_of(message) -> str:
    sender = getattr(message, "sender", None)
    return str(getattr(sender, "name", "") or getattr(sender, "qq", "") or "")


def _text_of(message) -> str:
    """正文；没有文本时用渲染占位（[图片]/[表情]），避免把纯图当成空话。"""
    text = str(getattr(message, "text", "") or "").strip()
    if text:
        return text
    try:
        from .models import render_body

        return render_body(message).strip()
    except Exception:  # noqa: BLE001 - 渲染失败就当没内容
        return ""


def _minutes_between(later: Optional[datetime], earlier: Optional[datetime]) -> float:
    if not later or not earlier:
        return 0.0
    try:
        return max(0.0, (later - earlier).total_seconds() / 60.0)
    except TypeError:
        return 0.0


def _looks_like_question(text: str) -> bool:
    body = str(text or "").strip()
    if len(body) < 2:
        return False
    return bool(_QUESTION_RE.search(body))


def read_pulse(
    messages: Iterable,
    *,
    now: Optional[datetime] = None,
    self_id: str = "",
) -> Pulse:
    """从这一轮拿到的消息（历史 + 当前）算局面。

    ``messages`` 按时间正序；自己的消息不计入「谁在说话」，但最后一条如果是自己
    发的，会体现在 ``SelfPosition`` 里而不是这里。
    """
    items = [m for m in (messages or []) if m is not None]
    others = [m for m in items if not _is_self(m)]
    if not others:
        return Pulse()

    pulse = Pulse()
    pulse.messages = len(others)
    pulse.speakers = len({_speaker_of(m) for m in others if _speaker_of(m)})

    first = next((m for m in others if getattr(m, "time", None)), None)
    last = next((m for m in reversed(others) if getattr(m, "time", None)), None)
    if last is not None and now is not None:
        pulse.quiet_minutes = _minutes_between(now, last.time)
    if first is not None and last is not None:
        pulse.span_minutes = _minutes_between(last.time, first.time)

    pulse.dialogue = pulse.speakers <= 2 and pulse.messages >= 4

    tail = others[-2:]
    pulse.chain = (
        len(tail) == 2 and _speaker_of(tail[0]) and _speaker_of(tail[0]) == _speaker_of(tail[1])
    )

    newest = others[-1]
    newest_text = _text_of(newest)
    pulse.last_text = newest_text[:40]
    pulse.ended = (
        len(newest_text) <= _ENDED_MAX_CHARS and bool(_ENDED_RE.search(newest_text))
    )
    pulse.low_energy = bool(_LOW_ENERGY_RE.match(newest_text))

    # 悬空提问：回看最后几条，找到最后一个提问者，看它之后有没有别人接话
    for index in range(len(others) - 1, max(-1, len(others) - 1 - _QUESTION_TAIL), -1):
        message = others[index]
        text = _text_of(message)
        if not _looks_like_question(text):
            continue
        answered = any(
            _speaker_of(later) != _speaker_of(message) for later in others[index + 1 :]
        )
        if answered:
            break
        pulse.question_id = str(getattr(message, "message_id", "") or "")
        pulse.question_who = _name_of(message)
        pulse.question_text = text[:50]
        break

    return pulse


def read_self_position(
    messages: Iterable,
    *,
    now: Optional[datetime] = None,
) -> SelfPosition:
    """算 Bot 自己在这个会话里的位置（上次说话 / 今天说了几次）。"""
    mine = [m for m in (messages or []) if m is not None and _is_self(m)]
    if not mine:
        return SelfPosition()

    position = SelfPosition()
    today = now.date() if now is not None else None
    if today is not None:
        position.today_count = sum(
            1 for m in mine if getattr(m, "time", None) and m.time.date() == today
        )
    else:
        position.today_count = len(mine)

    latest = next((m for m in reversed(mine) if getattr(m, "time", None)), None)
    if latest is not None and now is not None:
        position.last_minutes = _minutes_between(now, latest.time)
    position.last_text = _text_of(latest)[:40] if latest is not None else ""
    return position


__all__ = ["Pulse", "SelfPosition", "read_pulse", "read_self_position"]
