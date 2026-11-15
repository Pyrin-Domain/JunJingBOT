"""对话关系：这条消息是「谁对谁说的」。

为什么要单独算：
    上下文明明有十几条消息，模型却常常把「A 接 B 的话」当成「冲自己说的」。
    真实 QQ 群里大量消息既没有 @ 也没有引用，只是顺着上一条往下聊；只看每行的
    发言人，看不出这条是谁的回合。这类消息本该沉默，结果很容易硬接一句。

这套推断分四档（越靠前越确定）：
    at      显式 @ 了某人 —— 正文里已经渲染成 ``@昵称(QQ)``，不用再加标记；
    reply   引用了某条 —— 正文里已经有 ``↳``，只有原消息取不到时才需要占位；
    name    直接喊了 Bot 的名字（没 @）—— 行尾标 ``→我``；
    implicit 什么都没说，只是接着上一个人的话 —— 行尾标 ``→昵称``。

另外给主回复提供一段 ``<dialogue>``：几个人在聊、最后一句是谁对谁、Bot 是不是
旁听。以前只有插话（旁听视角）才有局面信息，正常回复时模型看不到这层。

本模块只做纯字符串 / 纯数据计算：不读配置、不发包，方便单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .attention import SUMMON, detect_attention

# 相邻两条间隔超过这个秒数，就不再当成「接上一句」
TURN_GAP_SECONDS = 180

# 别人至少说了几条，才值得给 <dialogue>（一两句的场景没必要占位置）
MIN_DIALOGUE_MESSAGES = 2
MAX_DIALOGUE_PEOPLE = 3
MAX_DIALOGUE_NAME = 12


def _sender_qq(message) -> str:
    return str(getattr(getattr(message, "sender", None), "qq", "") or "")


def _is_self(message) -> bool:
    return bool(getattr(getattr(message, "sender", None), "is_self", False))


def _short_name(message, names: Mapping[str, str]) -> str:
    sender = getattr(message, "sender", None)
    qq = _sender_qq(message)
    name = str(getattr(sender, "name", "") or names.get(qq) or qq or "未知").strip()
    return name[:MAX_DIALOGUE_NAME]


def _gap_ok(later, earlier) -> bool:
    """两条消息间隔是否还在同一个回合里（时间缺失时不限制）。"""
    first = getattr(earlier, "time", None)
    second = getattr(later, "time", None)
    if not isinstance(first, datetime) or not isinstance(second, datetime):
        return True
    try:
        return (second - first).total_seconds() <= TURN_GAP_SECONDS
    except TypeError:
        return True


def _explicit_targets(message, self_id: str) -> list[str]:
    """@ 的人（``@全体成员`` 不算，它是对所有人说的）。"""
    targets: list[str] = []
    for qq in getattr(message, "mentions", []) or []:
        value = str(qq or "")
        if value in ("", "all", "0") or value in targets:
            continue
        targets.append(value)
    return targets


def _reply_target(message) -> str:
    """引用/回复时不看内容，只看被引用那条的发送者。"""
    quote = getattr(message, "quote", None)
    if quote is not None:
        qq = str(getattr(getattr(quote, "sender", None), "qq", "") or "")
        if qq:
            return qq
    return ""


def _calls_me_by_name(message, self_id: str, aliases: Iterable[str]) -> bool:
    """没 @、没引用，但正文在直呼 Bot 的名字。"""
    if not self_id:
        return False
    try:
        attention = detect_attention(
            chat_type="group",
            text=str(getattr(message, "text", "") or ""),
            mentions_me=False,
            replies_to_me=False,
            aliases=aliases,
        )
    except Exception:  # noqa: BLE001 - 判定失败就当没喊
        return False
    return attention.level == SUMMON


@dataclass
class Addressee:
    """一条消息的指向。"""

    qq: str = ""            # 目标 QQ；空表示「对群里说」
    kind: str = ""          # at / reply / name / implicit / group
    label: str = ""         # 渲染用的短昵称（self 时是「我」）
    marker: bool = False    # 正文里已经能看出指向时不给标记

    @property
    def is_self(self) -> bool:
        return self.label == "我"


def resolve_addressees(
    messages: Iterable,
    names: Mapping[str, str] | None = None,
    *,
    self_id: str = "",
    aliases: Iterable[str] = (),
) -> list[Addressee]:
    """按时间正序给出每条消息的指向（与 ``messages`` 一一对应）。"""
    names = names or {}
    items = list(messages or [])
    me = str(self_id or "")
    out: list[Addressee] = []

    for index, message in enumerate(items):
        qq = _sender_qq(message)

        ats = _explicit_targets(message, me)
        if ats:
            label = "我" if me and me in ats else "、".join(
                (str(names.get(target) or target)[:MAX_DIALOGUE_NAME]) for target in ats
            )
            out.append(Addressee(qq=ats[0], kind="at", label=label))
            continue

        replied = _reply_target(message)
        if replied:
            label = "我" if me and replied == me else str(names.get(replied) or replied)[:MAX_DIALOGUE_NAME]
            out.append(Addressee(qq=replied, kind="reply", label=label))
            continue

        if getattr(message, "reply_to_id", "") or getattr(message, "quote", None):
            # 引用了但原消息取不到：关系照留，只是没有内容。
            # 行尾已经有「↳[id] 原消息已取不到」，不再额外加 → 标记。
            out.append(Addressee(kind="reply", label=""))
            continue

        if _calls_me_by_name(message, me, aliases):
            out.append(Addressee(qq=me, kind="name", label="我", marker=True))
            continue

        # 什么都没说：往回找最近一个「别人」，就是它在接谁的话
        target = ""
        target_name = ""
        for earlier in reversed(items[:index]):
            other = _sender_qq(earlier)
            if not other or other == qq:
                continue
            if not _gap_ok(message, earlier):
                break
            target = other
            target_name = _short_name(earlier, names)
            break
        if target and target != me:
            out.append(Addressee(qq=target, kind="implicit", label=target_name, marker=True))
        elif target:
            out.append(Addressee(qq=me, kind="implicit", label="我", marker=True))
        else:
            out.append(Addressee(kind="group"))
    return out


def addressee_markers(
    messages: Iterable,
    names: Mapping[str, str] | None = None,
    *,
    self_id: str = "",
    aliases: Iterable[str] = (),
) -> dict[str, str]:
    """``message_id -> 行尾标记``；只在正文看不出指向时才给（省 token）。

    历史里每行都会标（``→谁``），保持一致比「只标变化处」更好读：模型不需要
    逐行推理「这行没标是不是就没人接」。私聊由调用方跳过，那里不需要标记。
    """
    items = list(messages or [])
    marks: dict[str, str] = {}
    for message, addressee in zip(
        items,
        resolve_addressees(items, names, self_id=self_id, aliases=aliases),
        strict=False,
    ):
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or not addressee.marker or not addressee.label:
            continue
        if _is_self(message):
            continue  # 自己说过的话由 <me>/<dialogue> 负责，行尾不用重复标
        marks[message_id] = f"→{addressee.label}"
    return marks


@dataclass
class DialogueView:
    """一段对话的「谁在跟谁聊」。"""

    counts: list[tuple[str, int]] = field(default_factory=list)   # 别人：昵称 -> 条数（多的在前）
    self_count: int = 0
    last_from: str = ""
    last_to: str = ""          # "" 表示对群里说
    last_kind: str = ""

    @property
    def others(self) -> int:
        return sum(count for _, count in self.counts)

    def render(self) -> list[str]:
        if not self.others:
            return []
        lines: list[str] = []
        spoken = "、".join(f"{name} {count} 条" for name, count in self.counts[:MAX_DIALOGUE_PEOPLE])
        if len(self.counts) > MAX_DIALOGUE_PEOPLE:
            spoken += f" 等 {len(self.counts)} 人"
        if self.self_count:
            spoken += f"；我 {self.self_count} 条"
        lines.append(f"谁在说：{spoken}")
        if not self.last_from:
            return lines
        if not self.last_to:
            lines.append(f"最后一条：{self.last_from} 对群里说的")
        elif self.last_to == "我":
            lines.append(f"最后一条：{self.last_from} 对你说的")
        else:
            verb = "接着" if self.last_kind == "implicit" else "在回"
            lines.append(f"最后一条：{self.last_from} {verb} {self.last_to} 的话")
        if self.last_to and self.last_to != "我":
            lines.append(f"最后这条是冲 {self.last_to} 说的，不是冲你——没有点你就别抢话")
        return lines


def describe_dialogue(
    messages: Iterable,
    names: Mapping[str, str] | None = None,
    *,
    self_id: str = "",
    aliases: Iterable[str] = (),
) -> DialogueView:
    """统计这段对话的参与者和最后一句的指向。"""
    names = names or {}
    items = list(messages or [])
    if not items:
        return DialogueView()

    addressees = resolve_addressees(items, names, self_id=self_id, aliases=aliases)

    view = DialogueView()
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for message in items:
        if _is_self(message):
            view.self_count += 1
            continue
        key = _sender_qq(message) or _short_name(message, names)
        if key not in counts:
            counts[key] = 0
            shown[key] = _short_name(message, names)
        counts[key] += 1
    view.counts = sorted(
        ((shown[key], count) for key, count in counts.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )

    last = items[-1]
    last_addressee = addressees[-1]
    view.last_from = "我" if _is_self(last) else _short_name(last, names)
    view.last_kind = last_addressee.kind
    if last_addressee.kind in ("at", "reply", "name", "implicit"):
        view.last_to = last_addressee.label
    return view


__all__ = [
    "Addressee",
    "DialogueView",
    "MIN_DIALOGUE_MESSAGES",
    "TURN_GAP_SECONDS",
    "addressee_markers",
    "describe_dialogue",
    "resolve_addressees",
]
