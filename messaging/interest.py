"""话题兴趣匹配：这一段聊天命中了君景人设里的哪些点。

只做一件事：拿配置里的词表去匹配消息文本，命中的挑出来写进 ``<interest>``。
不猜、不扩展、不调模型——judge 用的是小模型，让它自己扫一遍词表又慢又容易漏。

一个重要约束：**兴趣不构成开口的理由。**
群里最让人烦的不是不说话的人，是每个自己感兴趣的话题都要插一嘴的人。
兴趣只用于「局面差不多的时候，更倾向接哪个话题」，判断该不该说的仍然是
「这一刻群里缺什么角色」（见 ``agent/prompts._INTERJECT_JUDGE_SYSTEM``）。
所以配置里留了 ``low``：命中了也不加分，只是提示模型「这个我懂一点」。

本模块只做纯字符串匹配：不读配置、不发包，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

HIGH = "high"
LOW = "low"


@dataclass(frozen=True)
class InterestHit:
    """一次命中：什么话题、权重、谁说的、原话。"""

    topic: str
    weight: str = HIGH
    who: str = ""
    text: str = ""


def _terms(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _body_of(message) -> str:
    text = str(getattr(message, "text", "") or "").strip()
    if text:
        return text
    try:
        from .models import render_body

        return render_body(message).strip()
    except Exception:  # noqa: BLE001
        return ""


def _name_of(message) -> str:
    sender = getattr(message, "sender", None)
    return str(getattr(sender, "name", "") or getattr(sender, "qq", "") or "")


def read_interests(
    messages: Iterable,
    *,
    high=(),
    low=(),
    limit: int = 3,
) -> list[InterestHit]:
    """从这一段消息里挑出命中的兴趣点，越近的越靠前。

    :param high: 明显在意的词（女装、独角兽…）
    :param low: 只是懂一点的词（作业、报错…）
    :param limit: 最多返回几条，避免把上下文撑长
    """
    high_terms = _terms(high)
    low_terms = _terms(low)
    if not high_terms and not low_terms:
        return []

    hits: list[InterestHit] = []
    seen: set[str] = set()
    for message in reversed(list(messages or [])):
        if message is None:
            continue
        sender = getattr(message, "sender", None)
        if getattr(sender, "is_self", False):
            continue      # 我自己说过的不算「别人在聊我在意的事」
        body = _body_of(message)
        if not body:
            continue
        lowered = body.lower()
        for weight, terms in ((HIGH, high_terms), (LOW, low_terms)):
            for term in terms:
                key = term.lower()
                if key in seen or key not in lowered:
                    continue
                seen.add(key)
                hits.append(
                    InterestHit(
                        topic=term,
                        weight=weight,
                        who=_name_of(message),
                        text=body[:40],
                    )
                )
                if len(hits) >= limit:
                    return hits
    return hits


def render_interests(hits: Iterable[InterestHit]) -> list[str]:
    """渲染成 ``<interest>`` 里的几行。"""
    lines: list[str] = []
    for hit in hits or ():
        who = f"{hit.who}：" if hit.who else ""
        note = "" if hit.weight == HIGH else "（略懂）"
        lines.append(f"命中：{hit.topic}{note} ← {who}「{hit.text}」")
    return lines


__all__ = ["HIGH", "LOW", "InterestHit", "read_interests", "render_interests"]
