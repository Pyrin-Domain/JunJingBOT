"""判断一条消息对 Bot 来说属于「哪一种注意力」，而不是「要不要回」。

为什么单独拆一层：
    ``ChatContext.directed_at_me`` 原本只有 @ / 引用 / 私聊三种，漏掉了 QQ 群最
    常见的召唤形态——**直接喊名字但没 @**（「君景呢」「君景在吗」「君景你觉得呢」）。
    这类消息既没有 @ 段、也不是引用，pipeline 的 AI 入口不认识它，插话器又把它当
    「没人叫我」处理，于是整条消息石沉大海。

三档语义：
    summon   当面叫它：私聊 / @我 / 引用我 / 直呼别名并在呼唤。
             这不是插话，是「迟到回复」，必须走正式回复，不许沉默。
    about_me 在聊它但没点名：提到别名、在讨论机器人。
             可以不接，但该竖耳朵——插话器的主要机会区。
    ambient  纯群体闲聊，跟它无关。插话门槛最高。

别名匹配故意保守：只有「剩余部分为空 / 只剩 1~2 字 / 以呼唤词开头」才算 summon，
「君景好可爱」这种背后夸属于 about_me，不强迫它回应。

本模块只做纯字符串判断：不读配置、不发包、不碰时间，方便单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SUMMON = "summon"
ABOUT_ME = "about_me"
AMBIENT = "ambient"

# 认名字用的默认别名（真实别名由配置 ``interject.aliases`` 覆盖或补充）
DEFAULT_ALIASES: tuple[str, ...] = ("君景", "独角兽")

# 别名后面跟着这些词，才算「在叫它」而不是「在说它」
_CALL_PREFIXES: tuple[str, ...] = (
    "在吗",
    "在不在",
    "在么",
    "在嘛",
    "在干嘛",
    "在干啥",
    "干嘛呢",
    "干什么",
    "出来",
    "过来",
    "说话",
    "理我",
    "理一下",
    "醒醒",
    "别装死",
    "帮我",
    "给我",
    "讲一下",
    "讲讲",
    "解释",
    "你觉得",
    "你说",
    "你看",
    # 第二人称：名字打头再「你…」，是在对它说话（「君景你最近忙啥」）。
    # 「君景好可爱」「君景这个机器人…」是第三人称评价，不该命中这里。
    "你",
    "您",
    # 疑问词打头：是在追问它（「君景怎么不说话」「君景是不是又熬夜了」）
    "怎么",
    "为啥",
    "啥",
    "是不是",
    "能不能",
    "会不会",
    "要不要",
    "有没有",
)

# 没提名字但明显在说机器人
_ROBOT_WORDS: tuple[str, ...] = (
    "机器人",
    "bot",
    "自动回复",
    "是不是人",
    "是真人吗",
    "ai吧",
)

# 标点 / 空白 / CQ 码：判断「剩余几个字」时不算数
_PUNCT_RE = re.compile(r"[\s\u3000，。！？、,.!?~～…—\-－（）()【】\[\]\"'“”‘’@]+")
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


@dataclass(frozen=True)
class Attention:
    """一条消息对 Bot 的关注等级。"""

    level: str = AMBIENT
    evidence: str = ""      # 凭什么这么判（写日志 / 写进 <scene> 都用得上）

    @property
    def is_summon(self) -> bool:
        return self.level == SUMMON

    @property
    def is_about_me(self) -> bool:
        return self.level == ABOUT_ME


# ══════════════════════════════════════════════════════════
# 从 event 取信号
# ══════════════════════════════════════════════════════════
def message_text(event: dict | None) -> str:
    """只取纯文本（不含 @ 段、不含图片占位），用于别名匹配。

    ``message`` 段缺失时退回 ``raw_message``，并剥掉 CQ 码——否则
    ``[CQ:at,qq=123]`` 会被当成正文去匹配别名。
    """
    event = event or {}
    parts: list[str] = []
    for seg in event.get("message") or []:
        if str(seg.get("type") or "") == "text":
            parts.append(str((seg.get("data") or {}).get("text") or ""))
    if not parts:
        raw = str(event.get("raw_message") or "")
        parts.append(_CQ_RE.sub("", raw))
    return "".join(parts).strip()


def mentions(event: dict | None, self_id) -> bool:
    """这条消息有没有 @ 指定的人（同步版，等价于 ``is_AT`` 但不发包）。"""
    if not self_id:
        return False
    me = str(self_id)
    for seg in (event or {}).get("message") or []:
        if str(seg.get("type") or "") != "at":
            continue
        if str((seg.get("data") or {}).get("qq") or "") == me:
            return True
    return False


def _aliases(extra) -> tuple[str, ...]:
    """默认别名 + 配置别名，去空去重，长的排前面（避免「君景」抢掉「君景酱」）。"""
    names = [str(x or "").strip() for x in (*DEFAULT_ALIASES, *(extra or ()))]
    uniq = [name for name in dict.fromkeys(names) if name]
    return tuple(sorted(uniq, key=len, reverse=True))


# ══════════════════════════════════════════════════════════
# 主判断
# ══════════════════════════════════════════════════════════
def detect_attention(
    *,
    chat_type: str = "group",
    text: str = "",
    mentions_me: bool = False,
    replies_to_me: bool = False,
    aliases=(),
) -> Attention:
    """判定注意力等级。

    :param chat_type: ``group`` / ``private``
    :param text: 消息纯文本（用 :func:`message_text` 取）
    :param mentions_me: 是否 @ 了 Bot
    :param replies_to_me: 是否引用了 Bot 说过的消息
    :param aliases: 额外别名（配置 ``interject.aliases``）
    """
    if str(chat_type or "") == "private":
        return Attention(SUMMON, "私聊")
    if mentions_me:
        return Attention(SUMMON, "@我")
    if replies_to_me:
        return Attention(SUMMON, "引用我的消息")

    body = str(text or "").strip()
    if not body:
        return Attention(AMBIENT)

    for alias in _aliases(aliases):
        if alias not in body:
            continue
        rest = _PUNCT_RE.sub("", body.replace(alias, "", 1))
        # 单独喊名字：只剩语气词也算（「君景呢」「君景！」）
        if len(rest) <= 2:
            return Attention(SUMMON, f"直呼「{alias}」")
        if rest.startswith(_CALL_PREFIXES):
            return Attention(SUMMON, f"直呼「{alias}」+ 呼唤")
        # 句子里提到了但没在叫 → 只在背后议论，插话器自己决定要不要凑
        return Attention(ABOUT_ME, f"提到「{alias}」")

    lowered = body.lower()
    for word in _ROBOT_WORDS:
        if word in lowered:
            return Attention(ABOUT_ME, f"提及「{word}」")
    return Attention(AMBIENT)


def detect_attention_of(
    event: dict | None,
    *,
    self_id="",
    replies_to_me: bool = False,
    aliases=(),
) -> Attention:
    """:func:`detect_attention` 的便利包装：直接从 NapCat event 取所有信号。"""
    event = event or {}
    return detect_attention(
        chat_type=str(event.get("message_type") or ""),
        text=message_text(event),
        mentions_me=mentions(event, self_id),
        replies_to_me=replies_to_me,
        aliases=aliases,
    )


__all__ = [
    "AMBIENT",
    "ABOUT_ME",
    "Attention",
    "DEFAULT_ALIASES",
    "SUMMON",
    "detect_attention",
    "detect_attention_of",
    "mentions",
    "message_text",
]
