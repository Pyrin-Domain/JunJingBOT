"""Agent 输出的解析与后处理（纯函数，不碰网络）。

模型输出协议：

    <status>…</status>
    <output>
      <message>消息1</message>
      <message>消息2</message>
      <next>结束</next>
    </output>

只有 ``<message>`` 里的文字会真正发出去；``<status>`` / 思考 仅用于日志。
另外这里还放着「别当复读机」的检测、高频场景台词池、插话判断解析。
"""

from __future__ import annotations

import difflib
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from messaging.meme import get_library, is_enabled as meme_enabled
from messaging.models import ChatContext  # 仅用于类型提示
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger("QQBotAgent.Output")

# ============================================================
# 结构化输出解析：<status> / 思考 / <output><message>
# ============================================================
# 只有 <message> 里的内容会被真正发出去，status/think 仅用于日志。
_BLOCK_RE = re.compile(r"<(status|think|next|output)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
# <next> 通常嵌在 <output> 里，会被 _BLOCK_RE 一并匹配掉，所以单独再抓一次
_NEXT_RE = re.compile(r"<next\b[^>]*>(.*?)</next\s*>", re.S | re.I)
_MESSAGE_RE = re.compile(r"<message\b([^>]*)>(.*?)</message\s*>", re.S | re.I)
_AT_RE = re.compile(r"<at>\s*(\d+)\s*</at>", re.I)
_STICKER_RE = re.compile(r"<sticker>\s*(.*?)\s*</sticker>", re.S | re.I)
_VOICE_RE = re.compile(r"<voice\b[^>]*>(.*?)</voice>", re.S | re.I)
_MEME_RE = re.compile(r"<meme\b[^>]*>(.*?)</meme\s*>", re.S | re.I)
_QUOTE_ATTR_RE = re.compile(r"""quote\s*=\s*["']?([^"'\s>]+)["']?""", re.I)
# 模型偶尔会漏写闭合标签 / 多写一层，这些残留标签不能被当成消息发出去
_STRAY_TAG_RE = re.compile(
    r"</?\s*(?:status|think|output|message|at|sticker|voice|action|meme)\b[^>]*>", re.I
)

# ── 续说（<next>继续/结束</next>）─────────────────────────────
# 一次 invoke 说的话常常不够：傲娇一句就收工、被催了才补答案。
# 让模型自己标 <next>，需要时再接一轮；这里限制次数，避免没完没了地刷屏。
_MAX_CONTINUE_ROUNDS = 2
_MAX_OUT_MESSAGES = 6
_NEXT_CONTINUE_VALUES = {"continue", "next", "继续", "接着", "1", "true", "yes", "是"}

# 这些工具一旦被调用，说明本轮内容已经用别的方式发出去了（不算沉默）
_ANSWER_TOOLS = {"send_message", "send_dress"}

# 有副作用的工具：已经执行过就不该换模型重跑（否则会重复发消息 / 重复禁言）
_SIDE_EFFECT_TOOLS = _ANSWER_TOOLS | {"poke", "emoji_like", "set_ban", "recall_msg"}

# 标记「这条是主人说的」的旧前缀（字符串接口兼容）
_DOM_PREFIX = "{isDom:true}"


def _render_meme(name: str) -> str:
    """把 ``<meme>名字</meme>`` 换成表情对应的 CQ 码。

    找不到就丢掉这一段：名字是模型自己写的，写错时发一个莫名其妙的占位文本
    比什么都不发更难解释。
    """
    if not meme_enabled():
        return ""
    try:
        code = get_library().cq(name)
    except Exception as exc:  # noqa: BLE001 - 表情包坏了不能影响回复
        logger.warning("[表情] 解析 %r 失败：%s", name, exc)
        return ""
    if not code:
        logger.info("[表情] 库里没有 %r，本条跳过", name)
    return code


def render_message_body(body: str) -> str:
    """把 <at> / <sticker> / <meme> / <voice> 段转成 NapCat 能识别的 CQ 码。"""
    body = _AT_RE.sub(lambda m: f"[CQ:at,qq={m.group(1)}]", body or "")
    body = _STICKER_RE.sub(lambda m: f"[CQ:image,file={m.group(1)}]", body)
    body = _MEME_RE.sub(lambda m: _render_meme(m.group(1)), body)
    body = _VOICE_RE.sub(lambda m: m.group(1), body)
    return _STRAY_TAG_RE.sub("", body).strip()


# 兼容旧名字（测试按旧的私有名导入过）
_render_message_body = render_message_body


def normalize_next(value: Optional[str]) -> str:
    """把 <next> 的写法归一成 continue / end；漏写按结束处理。"""
    raw = str(value or "").strip().lower()
    return "continue" if raw in _NEXT_CONTINUE_VALUES else "end"


_normalize_next = normalize_next


def parse_agent_output(text: str) -> dict:
    """把模型输出解析成 {"status", "think", "next", "messages", "raw"}。

    - status / think 只用于日志，不会发送；
    - messages 是真正要发出去的 QQ 消息（已转成 CQ 码，空串代表这条不发）。

    模型漏写 <output> 时会退化成「把 status/think 之外的正文当作一条回复」。
    """
    parsed: dict[str, Any] = {
        "status": "",
        "think": "",
        "next": "end",
        "messages": [],
        "raw": text or "",
    }
    if not text:
        return parsed

    blocks: dict[str, str] = {}
    for name, content in _BLOCK_RE.findall(text):
        blocks.setdefault(name.lower(), content)

    parsed["status"] = (blocks.get("status") or "").strip()
    parsed["think"] = (blocks.get("think") or "").strip()
    next_match = _NEXT_RE.search(text)
    parsed["next"] = _normalize_next(
        next_match.group(1) if next_match else blocks.get("next")
    )

    output_block = blocks.get("output")
    if output_block is None:
        output_block = _BLOCK_RE.sub("", text)
    output_block = (output_block or "").strip()

    found = _MESSAGE_RE.findall(output_block)
    if not found:
        if output_block:
            parsed["messages"] = [_render_message_body(output_block)]
        return parsed

    messages: list[str] = []
    for attrs, body in found:
        content = _render_message_body(body)
        quote = _QUOTE_ATTR_RE.search(attrs or "")
        if quote and content:
            content = f"[CQ:reply,id={quote.group(1)}] {content}"
        messages.append(content)

    # 模型偶尔把 <meme> 写在 </message> 后面（而不是里面）。这种游离标签
    # 以前会被 _STRAY_TAG_RE 当垃圾丢掉，等于这一轮白写。这里补到最后一条，
    # 保证「表情包已经生成了」就真的能发出去。
    stray_memes = _MEME_RE.findall(_MESSAGE_RE.sub("", output_block))
    already_has_meme = any(
        "[CQ:mface" in message or "[CQ:face" in message for message in messages
    )
    if stray_memes and not already_has_meme:
        extra = _render_meme(stray_memes[0])
        if extra:
            if messages and messages[-1]:
                messages[-1] = f"{messages[-1]}{extra}"
            else:
                messages.append(extra)

    parsed["messages"] = messages
    return parsed


def split_sendable_messages(parsed: dict) -> list[str]:
    """过滤掉空的 <message>，得到真正要发送的消息列表。"""
    return [m for m in parsed.get("messages") or [] if m and m.strip()]


# ============================================================
# 复读检测：示例台词照抄 / 重复自己最近说过的话
# ============================================================
# 小模型很容易把提示词里的示例台词原样搬出来（「喏，就这一张，看完了不许笑。」），
# 于是维护一份「示例台词表」，输出命中就送去改写一次。
_NOISE_RE = re.compile(
    r"[\s，。！？~～、,.!?…—\-—·：:;；\"'“”‘’()（）\[\]【】<>《》]+"
)


def normalize_text(text: str) -> str:
    return _NOISE_RE.sub("", text or "")


_normalize = normalize_text


def collect_template_lines(prompt: str) -> tuple[str, ...]:
    """从 SYSTEM_PROMPT 里抽出所有示例台词（<message> 内容 + 语言风格示例的回答）。

    自动抽取，保证与提示词同步：改示例不用改代码。
    格式说明里出现的 <message> 标签（很长、含 begin{/end{/**）会被过滤掉。
    """
    lines: list[str] = []
    # 只从「对话示例」段里取 <message> 台词；输出格式段里的 <message> 是格式占位符，不是台词
    demo = prompt or ""
    if "begin{对话示例}" in demo:
        demo = demo[demo.index("begin{对话示例}"):]
    for _attrs, body in _MESSAGE_RE.findall(demo):
        body = _render_message_body(body)
        if not body or len(body) > 40:
            continue
        if re.search(r"begin\{|end\{|\*\*|send_|当前上下文", body):
            continue
        lines.append(body)
    lines.extend(re.findall(r"^回答[:：]\s*(.+)$", prompt or "", re.M))
    # 去重、去掉太短的（容易误判）
    seen: dict[str, None] = {}
    for line in lines:
        norm = _normalize(line)
        if len(norm) >= 6:
            seen.setdefault(norm, None)
    return tuple(seen)


TEMPLATE_LINES: tuple[str, ...] = collect_template_lines(SYSTEM_PROMPT)


def _too_similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 8 and (a in b or b in a):
        return True
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.5:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def looks_like_repeat(text: str, recent: list[str] | None = None) -> bool:
    """判断这句话是不是在照抄示例台词，或者和自己最近说过的话雷同。

    故意做得「宁可放过」：只有高度相似（完全一致 / 互相包含 / 相似度 ≥ 0.8）才判真。
    """
    norm = _normalize(text)
    if len(norm) < 6 or "[CQ:" in (text or ""):
        return False

    for template in TEMPLATE_LINES:
        if _too_similar(norm, template):
            return True

    for old in recent or []:
        old_norm = _normalize(old)
        if len(old_norm) >= 6 and _too_similar(norm, old_norm):
            return True
    return False


# ============================================================
# 高频场景备选台词
# ============================================================
# 小模型在「男娘 / 女装照」这类场景里几乎必抄示例台词（实测抄写率 ~70%）。
# 这些场景的台词池是手写的，被抓到复读时直接换一句没用过的，省掉一次 LLM 调用。
_SCENE_LINES: tuple[tuple[str, Optional[bool], tuple[str, ...]], ...] = (
    # (触发正则, 是否需要主人，None=都适用, 台词池)
    (
        r"男娘|南梁|nn",
        None,
        (
            "哈？人家是独角兽，货真价实的那种。",
            "又说这个，你们群是不是没别的梗了。",
            "行吧行吧，你说是就是，反正好看就行。",
            "喂喂，独角兽跟男娘差了十万八千里吧。",
            "嗯？我看你是想被角顶一下。",
            "打住打住，再说人家要认真生气了。",
        ),
    ),
    (
        r"女装|腿照|看看腿|白丝|黑丝|裙子照|发张|发一张|来一张|来个图|发个图|看看照片",
        True,
        (
            "喏，就一张，看完还我。",
            "拿好，别外传。",
            "这回只给一张，适可而止哦。",
            "偷偷给你看的，别声张。",
            "笑什么笑，很普通的啦。",
            "看完了要夸人家好看才行。",
        ),
    ),
    (
        r"女装|腿照|看看腿|白丝|黑丝|裙子照|发张|发一张|来一张|来个图|发个图|看看照片",
        False,
        (
            "想得美，找主人要去。",
            "不给，这个得主人点头。",
            "你算哪位呀，先排队吧。",
            "哦，那你就慢慢等着吧。",
            "不给不给，别缠着人家。",
            "这种要求人家一律不理的哦。",
        ),
    ),
)


def pick_scene_line(
    user_message: str, is_dom: bool, recent: list[str] | None = None
) -> Optional[str]:
    """高频场景的备选台词：优先挑一句最近没说过的。没有匹配的场景返回 None。"""
    text = (user_message or "").replace(_DOM_PREFIX, "")
    recent_norm = {_normalize(r) for r in (recent or [])}
    for pattern, need_dom, lines in _SCENE_LINES:
        if need_dom is not None and need_dom != is_dom:
            continue
        if re.search(pattern, text):
            fresh = [line for line in lines if _normalize(line) not in recent_norm]
            return random.choice(fresh or list(lines))
    return None


# ============================================================
# 敷衍检测 / 插话判断
# ============================================================
# 说完等于没说的典型句子（去掉标点后很短才算，避免误伤正常回复）
_DODGE_RE = re.compile(
    r"(在[.。…~\s]*在?说(话)?了?|人家在呢|在的在的|马上说|这就说|让我想想|等我一下|别急嘛?)"
)


def looks_like_dodge(messages: list[str]) -> bool:
    """判断这一批回复是不是「敷衍了事」：短 + 只有客套/拖延，没有实际内容。"""
    text = re.sub(r"[\s，,。.！!？?~～…、]+", "", "".join(messages or []))
    if not text or len(text) > 16:
        return False
    return bool(_DODGE_RE.search(text))


_looks_like_dodge = looks_like_dodge


_JUDGE_RE = re.compile(r"<judge>\s*([^<]{1,12})\s*</judge>", re.I)
_JUDGE_YES = ("yes", "y", "true", "1", "是", "可以", "能", "该", "插")


def parse_interject_judge(text: str) -> bool:
    """解析旧版 yes/no 判断结果（保留兼容）；拿不准一律当 no。"""
    raw = (text or "").strip()
    if not raw:
        return False
    matched = _JUDGE_RE.search(raw)
    if matched:
        token = matched.group(1).strip().lower()
    else:
        token = raw.splitlines()[0].strip().lower()
    return any(token.startswith(word) for word in _JUDGE_YES)


_parse_interject_judge = parse_interject_judge


# ── 插话：动作判断（silent / nod / join / answer / revive）──────────
INTERJECT_ACTS = ("silent", "nod", "join", "answer", "revive")

_ACT_RE = re.compile(r"<act>\s*([a-z_]{3,12})\s*</act>", re.I)
_WHY_RE = re.compile(r"<why>(.*?)</why>", re.I | re.S)
_REPLY_TO_RE = re.compile(r"<reply_to>\s*(\d{1,20})\s*</reply_to>", re.I)

# 小模型有时会把提示词里的占位说明原样抄进 <why>（日志里那句「一句话理由」
# 就是这么来的）。这种理由零信息量，当成空处理，看日志时一眼能发现模型没听话。
_PLACEHOLDER_WHY = (
    "一句话理由",
    "这里填具体理由",
    "具体理由",
    "理由",
    "原因",
    "动作",
    "这里填动作",
    "省略",
    "...",
    "……",
)


def _clean_why(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip().strip("。.，,～~ ")
    if not value or value in _PLACEHOLDER_WHY:
        return ""
    return value[:80]


@dataclass
class InterjectPlan:
    """插话的一次决定：做什么动作、为什么、引用哪条、要发什么。

    ``act`` 的含义：
        silent  不说话（绝大多数情况）
        nod     不说话，只做表情回应 / 戳一戳（想在场但没内容）
        join    接一句
        answer  回答一个没人接的问题
        revive  主动开新话题（极少）
    """

    act: str = "silent"
    why: str = ""
    reply_to: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def should_speak(self) -> bool:
        return self.act in ("join", "answer", "revive") and bool(self.messages)


def parse_interject_verdict(text: str) -> InterjectPlan:
    """解析插话判断结果。

    优先认 ``<act>``；模型退回旧格式（``<judge>yes</judge>`` / 只写 yes/no）时
    降级成 join / silent。认不出来一律 silent —— 插话宁可少一句，不要乱说一句。
    """
    raw = (text or "").strip()
    if not raw:
        return InterjectPlan()

    act = ""
    matched = _ACT_RE.search(raw)
    if matched:
        candidate = matched.group(1).strip().lower()
        if candidate in INTERJECT_ACTS:
            act = candidate
    if not act:
        act = "join" if parse_interject_judge(raw) else "silent"

    why = ""
    reason = _WHY_RE.search(raw)
    if reason:
        why = _clean_why(reason.group(1))

    reply_to = ""
    target = _REPLY_TO_RE.search(raw)
    if target:
        reply_to = target.group(1)

    return InterjectPlan(act=act, why=why, reply_to=reply_to)


_parse_interject_verdict = parse_interject_verdict


def has_side_effect_call(calls: list[dict] | None = None) -> bool:
    """本轮（当前 async Task）是否已经执行过有副作用的工具。"""
    if calls is None:
        from .tools.registry import current_tool_calls

        calls = current_tool_calls()
    return any((call or {}).get("tool") in _SIDE_EFFECT_TOOLS for call in calls)


_has_side_effect_call = has_side_effect_call

__all__ = [
    "TEMPLATE_LINES",
    "ChatContext",
    "collect_template_lines",
    "has_side_effect_call",
    "looks_like_dodge",
    "looks_like_repeat",
    "normalize_next",
    "normalize_text",
    "parse_agent_output",
    "parse_interject_judge",
    "parse_interject_verdict",
    "InterjectPlan",
    "INTERJECT_ACTS",
    "pick_scene_line",
    "render_message_body",
    "split_sendable_messages",
]
