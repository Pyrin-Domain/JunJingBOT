"""喂给 LLM 的聊天上下文：结构化数据模型 + 紧凑文本渲染。

旧实现把每条消息拼成散文：``消息发送者X，其QQ号为N，发送消息[message_id:M]:...``，
引用/转发再用中文句子嵌套，图片 URL 直接内联。带来几个问题：

* 模板本身每条约 40 字，8 条历史就是 300+ 字纯噪音；
* 引用一层层嵌套，同一句话在历史里出现两三次；
* 当前消息会在「历史」里再出现一遍，模型分不清哪条是要回的；
* 图片 URL 一个几百字符，历史里的 URL 根本用不上，纯烧 token；
* message_id 埋在散文里，模型容易抄错，撤回到错的楼层。

新做法分三层，互不耦合：

1. **采集**：NapCat event -> :class:`ChatMessage`（sender / segments / quote / forward 都是字段）；
2. **组装**：:class:`ChatContext` 持有场景元信息 + 历史 + 当前消息，并按预算裁剪；
3. **渲染**：固定行式文本，一行一条消息，字段顺序固定，与 SYSTEM_PROMPT 里的说明一一对应。

渲染结果示例::

    <scene>
    群聊 群号=20001 群名=君景的独角兽馆
    我=君景(10002)
    主人=Pyrin Domain(10001)
    现在=2026-10-18 21:04
    </scene>
    <history omitted="0">
    21:02 [1059815600] Pyrin Domain(10001,主人)：看看腿
    21:03 [1059815601] 我(10002)：[图片]
    21:03 [1059815614] Pyrin Domain(10001,主人) @我：你好
    </history>
    <current>
    21:04 [1059815617] Pyrin Domain(10001,主人) [回复 1059815600 Pyrin Domain：看看腿]：今天吃什么
    </current>

约束：本模块只做纯数据与纯文本，不碰网络、不读配置，方便单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Optional

from .interest import render_interests
from .pulse import read_pulse, read_self_position

# ── 渲染预算（按字符算，中文大约 1 字符 ≈ 1 token） ──────────────
MAX_NAME_CHARS = 16          # 昵称太长会挤掉正文
MAX_HISTORY_MESSAGES = 12    # 最多带多少条历史
MAX_HISTORY_CHARS = 2000     # 历史总预算
MAX_MESSAGE_CHARS = 300      # 单条历史消息正文上限
MAX_CURRENT_CHARS = 800      # 当前消息正文上限（要回应，给宽一点）
MAX_QUOTE_CHARS = 80         # 被引用消息的摘要
MAX_FORWARD_MESSAGES = 20    # 展开的转发最多几条
MAX_FORWARD_CHARS = 1200     # 展开的转发总预算
MAX_FORWARD_ITEM_CHARS = 160 # 转发里单条正文上限

# 旧版「上下文模板」的默认值：升级后这些内容已经由结构化场景块承担，
# 如果用户的配置里还是这些原文，就当没填，避免同一句话出现两遍。
LEGACY_CONTEXT_NOTES = (
    "当前是群聊，群号 {group_id}，发消息的用户 QQ 号是 {user_id}，"
    "消息 ID 是 {message_id},AT的对象的QQ号是{ated_user},"
    "你的QQ号是{self_id},主人的QQ号是{master_id}",
    "当前是私聊，用户 QQ 号是 {user_id}",
)


def clip(text: str, limit: int) -> str:
    """按字符截断，超出部分标注省略了多少字（比直接砍掉更容易让模型知道有缺失）。"""
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit}字)"


def is_legacy_context_note(text: str) -> bool:
    """判断一段「场景说明」是不是旧版模板的默认值（是则忽略）。"""
    norm = " ".join(str(text or "").split())
    return any(norm == " ".join(item.split()) for item in LEGACY_CONTEXT_NOTES)


# ══════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════
@dataclass
class Sender:
    """消息发送者。name 是群名片优先、昵称兜底；渲染时还会用 NameBook 补全。"""

    qq: str = ""
    name: str = ""
    is_self: bool = False
    is_master: bool = False

    def label(self, names: Optional[Mapping[str, str]] = None) -> str:
        """渲染成 ``昵称(QQ,主人)``；自己固定叫「我」，避免模型认错发言人。"""
        if self.is_self:
            return f"我({self.qq})" if self.qq else "我"
        name = str(self.name or (names or {}).get(self.qq) or "").strip()
        name = clip(name, MAX_NAME_CHARS)
        if not self.qq:
            return name or "未知"
        role = ",主人" if self.is_master else ""
        return f"{name or '未知'}({self.qq}{role})"


@dataclass
class Segment:
    """消息里的一个片段，保留原始顺序（文本、@、图片可能交替出现）。"""

    type: str = "text"
    text: str = ""
    qq: str = ""
    url: str = ""


@dataclass
class Quote:
    """被引用/回复的那条消息，只保留一行摘要，不再递归展开。

    images 保留被引用消息的图片 URL（不渲染进文本），供 OCR 等工具按需解析。
    """

    message_id: str = ""
    sender: Sender = field(default_factory=Sender)
    summary: str = ""
    images: list[str] = field(default_factory=list)


@dataclass
class ChatMessage:
    """一条聊天消息（转发里的子消息也用同一个结构）。"""

    message_id: str = ""
    time: Optional[datetime] = None
    sender: Sender = field(default_factory=Sender)
    segments: list[Segment] = field(default_factory=list)
    quote: Optional[Quote] = None
    forward: list["ChatMessage"] = field(default_factory=list)
    forward_total: int = 0      # 转发原始条数（可能大于 forward 里的条数，因为做了截断）
    miniapp: str = ""           # 小程序卡片的标题/描述

    @property
    def text(self) -> str:
        """纯文本部分（不含 @ / 图片占位），用于意图匹配与日志。"""
        return "".join(s.text for s in self.segments if s.type == "text").strip()

    @property
    def images(self) -> list[str]:
        return [s.url for s in self.segments if s.type == "image" and s.url]

    @property
    def mentions(self) -> list[str]:
        return [s.qq for s in self.segments if s.type == "at" and s.qq]

    def to_dict(self) -> dict:
        data = {
            "message_id": self.message_id,
            "time": self.time.strftime("%Y-%m-%d %H:%M:%S") if self.time else "",
            "sender": {
                "qq": self.sender.qq,
                "name": self.sender.name,
                "is_self": self.sender.is_self,
                "is_master": self.sender.is_master,
            },
            "text": self.text,
            "images": len(self.images),
        }
        if self.quote:
            data["quote"] = {
                "message_id": self.quote.message_id,
                "qq": self.quote.sender.qq,
                "summary": self.quote.summary,
            }
        if self.forward_total or self.forward:
            data["forward_total"] = self.forward_total or len(self.forward)
        if self.miniapp:
            data["miniapp"] = self.miniapp
        return data


# ── 事件 -> 消息 ────────────────────────────────────────────
_MEDIA_LABELS = {
    "face": "[表情]",
    "mface": "[表情]",
    "marketface": "[表情]",
    "video": "[视频]",
    "record": "[语音]",
    "music": "[音乐]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "location": "[位置]",
    "contact": "[名片]",
    "share": "[链接]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "forward": "[转发]",
    "xml": "[卡片]",
    "json": "[卡片]",
}


def parse_event(
    event: dict,
    *,
    self_id: str = "",
    master_ids: Iterable[str] = (),
    miniapp: str = "",
    now: Optional[datetime] = None,
) -> ChatMessage:
    """把一条 NapCat 消息事件转成 :class:`ChatMessage`（不发起任何网络请求）。

    引用消息、转发的展开由采集层（MessageProcessor）负责填进 quote / forward 字段。
    """
    event = event or {}
    sender_raw = event.get("sender") or {}
    qq = str(event.get("user_id") or sender_raw.get("user_id") or "")
    name = str(sender_raw.get("card") or sender_raw.get("nickname") or "")
    masters = {str(x) for x in master_ids if str(x)}
    sender = Sender(
        qq=qq,
        name=name,
        is_self=bool(self_id) and qq == str(self_id),
        is_master=bool(qq) and qq in masters,
    )

    ts = event.get("time")
    time = now
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            time = datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            time = now

    segments: list[Segment] = []
    for seg in event.get("message") or []:
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if seg_type == "text":
            segments.append(Segment("text", text=str(data.get("text") or "")))
        elif seg_type == "at":
            segments.append(Segment("at", qq=str(data.get("qq") or "")))
        elif seg_type == "image":
            segments.append(
                Segment("image", url=str(data.get("url") or ""), text=str(data.get("summary") or ""))
            )
        elif seg_type == "file":
            segments.append(Segment("file", text=str(data.get("name") or data.get("file") or "")))
        elif seg_type in ("mface", "marketface"):
            # 商城表情包：summary 形如「[嘬嘬]」，留下名字，渲染时能显示出来
            segments.append(Segment("mface", text=str(data.get("summary") or "")))
        elif seg_type == "reply":
            continue  # 引用关系单独存进 ChatMessage.quote
        elif seg_type == "forward":
            continue  # 旧版转发由采集层展开
        elif seg_type == "json":
            continue  # 小程序/转发卡片由采集层识别
        elif seg_type:
            segments.append(Segment(seg_type))

    # 拿不到 message 段时（部分历史接口只回 raw_message），退化成纯文本
    if not segments and event.get("raw_message"):
        segments.append(Segment("text", text=str(event.get("raw_message"))))

    return ChatMessage(
        message_id=str(event.get("message_id") or event.get("real_id") or ""),
        time=time,
        sender=sender,
        segments=segments,
        miniapp=miniapp,
    )


# ══════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════
def _mention_label(qq: str, names: Mapping[str, str]) -> str:
    if qq in ("all", "0"):
        return "@全体成员"
    name = clip(str(names.get(qq) or "").strip(), MAX_NAME_CHARS)
    if name == "我":
        return "@我"
    return f"@{name}({qq})" if name else f"@{qq}"


def render_body(msg: ChatMessage, names: Optional[Mapping[str, str]] = None) -> str:
    """只渲染正文（图片/@/媒体占位），不带时间与发送者。"""
    names = names or {}
    if msg.miniapp:
        return f"[小程序] {msg.miniapp}"
    out: list[str] = []
    for seg in msg.segments:
        if seg.type == "text":
            out.append(seg.text)
        elif seg.type == "image":
            out.append("[图片]")
        elif seg.type == "at":
            out.append(_mention_label(seg.qq, names))
        elif seg.type == "file":
            out.append(f"[文件 {seg.text}]" if seg.text else "[文件]")
        elif seg.type == "mface":
            # 别人的表情包带上名字，模型才知道群里常用哪几个
            name = re.sub(r"^[\[【(（]+|[\]】)）]+$", "", seg.text or "").strip()
            out.append(f"[表情:{name}]" if name else "[表情]")
        elif seg.type == "forward_card":
            out.append("[转发]")
        else:
            out.append(_MEDIA_LABELS.get(seg.type, f"[{seg.type}]"))
    # @ 和正文之间常有多个空格（NapCat 原文如此），压成一个
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def message_summary(msg: ChatMessage, limit: int = MAX_QUOTE_CHARS) -> str:
    """被引用消息的一行摘要；空消息也要给个占位，别渲染成空白。"""
    body = render_body(msg)
    if not body:
        if msg.forward_total or msg.forward:
            body = f"[转发 {msg.forward_total or len(msg.forward)} 条]"
        else:
            body = "[消息]"
    return clip(body, limit)


def _time_label(msg_time: Optional[datetime], now: Optional[datetime]) -> str:
    if not msg_time:
        return ""
    if now and msg_time.date() != now.date():
        return msg_time.strftime("%m-%d %H:%M")
    return msg_time.strftime("%H:%M")


def render_message(
    msg: ChatMessage,
    names: Optional[Mapping[str, str]] = None,
    *,
    now: Optional[datetime] = None,
    limit: int = MAX_MESSAGE_CHARS,
) -> str:
    """渲染一条消息（不含转发子消息展开）。

    行格式：``时间 [message_id] 昵称(QQ,主人) 提及：正文　[回复 ...]``
    """
    names = names or {}
    head: list[str] = []
    stamp = _time_label(msg.time, now)
    if stamp:
        head.append(stamp)
    if msg.message_id:
        head.append(f"[{msg.message_id}]")
    head.append(msg.sender.label(names))

    body = clip(render_body(msg, names), limit)
    line = " ".join(head)
    total = msg.forward_total or len(msg.forward)
    tag = f"【转发聊天记录 {total} 条】" if total else ""
    if body:
        line += f"：{body}" + (f" {tag}" if tag else "")
    elif tag:
        line += f" {tag}"
    else:
        line += "："

    if msg.quote and (msg.quote.summary or msg.quote.message_id):
        quote_who = msg.quote.sender.label(names)
        prefix = f"[{msg.quote.message_id}] " if msg.quote.message_id else ""
        line += f"　↳{prefix}{quote_who}：{clip(msg.quote.summary, MAX_QUOTE_CHARS)}"
    return line


def render_forward(msg: ChatMessage, names: Mapping[str, str], *, now: Optional[datetime] = None) -> list[str]:
    """展开转发里的子消息；超出条数/预算就截断并注明还剩多少条。"""
    if not msg.forward:
        return []
    lines: list[str] = []
    used = 0
    shown = 0
    for item in msg.forward:
        if shown >= MAX_FORWARD_MESSAGES or used >= MAX_FORWARD_CHARS:
            break
        line = render_message(item, names, now=now, limit=MAX_FORWARD_ITEM_CHARS)
        lines.append(f"    {shown + 1}. {line}")
        used += len(line)
        shown += 1
    total = msg.forward_total or len(msg.forward)
    rest = total - shown
    if rest > 0:
        lines.append(f"    …（还有 {rest} 条未展开）")
    return lines


@dataclass
class ChatContext:
    """一轮对话的完整上下文：场景 + 历史 + 当前消息。"""

    chat_type: str = "group"
    group_id: str = ""
    group_name: str = ""
    self_id: str = ""
    self_name: str = ""
    master_ids: list[str] = field(default_factory=list)
    now: datetime = field(default_factory=datetime.now)
    history: list[ChatMessage] = field(default_factory=list)
    current: ChatMessage = field(default_factory=ChatMessage)
    note: str = ""                      # 可选的人工补充说明（来自 prompts.*_context）
    attention: str = ""                 # summon / about_me / ambient，由 messaging.attention 判定
    attention_evidence: str = ""        # 凭什么这么判（日志 / 调试用）
    interest_hits: list = field(default_factory=list)  # 命中的兴趣点（由插话服务填充）
    max_history_messages: int = MAX_HISTORY_MESSAGES
    max_history_chars: int = MAX_HISTORY_CHARS

    # -------- 指向性判断 --------
    @property
    def attention_level(self) -> str:
        """注意力等级；没被显式判定过就从 directed_at_me 兜底推断。"""
        if self.attention:
            return self.attention
        return "summon" if self.directed_at_me else "ambient"

    @property
    def mentions_me(self) -> bool:
        """当前消息里有没有 @ 我。"""
        if not self.self_id:
            return False
        me = str(self.self_id)
        return any(str(qq) == me for qq in self.current.mentions)

    @property
    def replies_to_me(self) -> bool:
        """当前消息是不是在回复/引用我的某条消息。"""
        quote = self.current.quote
        if not quote or not self.self_id:
            return False
        return str(quote.sender.qq or "") == str(self.self_id)

    @property
    def directed_at_me(self) -> bool:
        """私聊 / @我 / 回复我 / 直呼我 —— 属于「当面跟我说」，不允许沉默。

        最后一项（直呼）由 :mod:`messaging.attention` 判定后写进 ``attention``：
        QQ 群里「君景呢」「君景在吗」这种喊名字不 @ 的消息，占了召唤场景的一大半，
        只认 @ 和引用会把它们全部漏掉。

        群里的普通闲聊即使主人说了一句，也不强制回复（Bot 可能只是在潜水）。
        """
        return (
            self.chat_type == "private"
            or self.mentions_me
            or self.replies_to_me
            or self.attention == "summon"
        )

    # -------- 名字表 --------
    def namebook(self) -> dict[str, str]:
        """QQ -> 昵称。历史里出现过的人都能补上名字，@ 的时候不至于只有号码。"""
        names: dict[str, str] = {}

        def feed(msg: ChatMessage) -> None:
            if msg.sender.qq and msg.sender.name and not msg.sender.is_self:
                names.setdefault(msg.sender.qq, msg.sender.name)
            if msg.quote and msg.quote.sender.qq and msg.quote.sender.name:
                names.setdefault(msg.quote.sender.qq, msg.quote.sender.name)
            for child in msg.forward:
                feed(child)

        for item in [*self.history, self.current]:
            feed(item)
        # @ 自己的时候直接写「@我」，比一串数字好认
        if self.self_id:
            names[self.self_id] = "我"
        return names

    def _self_label(self, names: Mapping[str, str]) -> str:
        name = str(self.self_name or "").strip()
        if not name:
            for msg in [self.current, *reversed(self.history)]:
                if msg.sender.is_self and msg.sender.name:
                    name = msg.sender.name
                    break
        name = clip(name, MAX_NAME_CHARS)
        if not self.self_id:
            return name or "我"
        # 昵称本身常带括号（如「君景(继伯）」），用 / 分隔避免出现两层括号
        return f"{name}/{self.self_id}" if name else self.self_id

    def _master_label(self, names: Mapping[str, str]) -> str:
        parts = []
        for qq in self.master_ids:
            name = clip(str(names.get(str(qq)) or "").strip(), MAX_NAME_CHARS)
            parts.append(f"{name}/{qq}" if name else str(qq))
        return "、".join(parts) or "无"

    # -------- 分块渲染 --------
    def render_scene(self, names: Optional[Mapping[str, str]] = None) -> list[str]:
        names = names if names is not None else self.namebook()
        lines = ["<scene>"]
        if self.chat_type == "group":
            head = f"群聊 群号={self.group_id or '未知'}"
            if self.group_name:
                head += f" 群名={self.group_name}"
            lines.append(head)
            lines.append(f"我={self._self_label(names)}")
            lines.append(f"主人={self._master_label(names)}")
        else:
            # 私聊：对方就是这一轮的对话人，直接把身份标出来
            lines.append(f"私聊 对方={self.current.sender.label(names)}")
            lines.append(f"我={self._self_label(names)}")
        lines.append(f"现在={self.now.strftime('%Y-%m-%d %H:%M')}")
        if self.attention == "summon" and not (self.mentions_me or self.replies_to_me):
            # 喊名字但没 @：这句就是在对它说，别装没听见
            lines.append("叫我=有人在直呼你名字（没有 @），这句是对你说的，要回一句")
        note = str(self.note or "").strip()
        if note and not is_legacy_context_note(note):
            lines.append(f"备注={note}")
        lines.append("</scene>")
        return lines

    def render_history(self, names: Optional[Mapping[str, str]] = None) -> list[str]:
        """按预算从最新往回挑，返回按时间正序的行（调用方自己套 <history> 标签）。"""
        names = names if names is not None else self.namebook()
        picked: list[str] = []
        used = 0
        omitted = 0
        total = len(self.history)
        for index in range(total - 1, -1, -1):
            msg = self.history[index]
            line = render_message(msg, names, now=self.now)
            if picked and (
                len(picked) >= self.max_history_messages or used + len(line) > self.max_history_chars
            ):
                omitted = index + 1
                break
            picked.append(line)
            used += len(line)
        picked.reverse()
        if omitted:
            picked.insert(0, f"…（更早 {omitted} 条已省略）")
        return picked

    def render_current(self, names: Optional[Mapping[str, str]] = None) -> list[str]:
        names = names if names is not None else self.namebook()
        lines = [render_message(self.current, names, now=self.now, limit=MAX_CURRENT_CHARS)]
        lines.extend(render_forward(self.current, names, now=self.now))
        return lines

    def render(self) -> str:
        """完整上下文文本：场景 → 历史 → 当前消息。"""
        names = self.namebook()
        lines = self.render_scene(names)
        history = self.render_history(names)
        if history:
            lines.append("<history>")
            lines.extend(history)
            lines.append("</history>")
        lines.append("<current>")
        lines.extend(self.render_current(names))
        lines.append("</current>")
        return "\n".join(lines)

    def render_history_only(self) -> str:
        return "\n".join(self.render_history())

    def render_as_bystander(self) -> str:
        """插话（旁听）视角的渲染——和 :meth:`render` 不是一回事。

        三处刻意的区别：

        1. 最新那条叫 ``<tail>`` 不叫 ``<current>``。``<current>`` 在正常回复里的
           语义是「本轮要回应的那条」，插话场景里它只是「最后一条」，没有特权；
           沿用 ``<current>`` 会让模型下意识地想去回它（哪怕它只是「那我去睡了」）。
        2. 多了 ``<me>``：我上次在这个群说了什么、多久之前、今天说过几次。
           没有这段，模型会在自己刚说完话之后又接一句，这是显得烦的第一大来源。
        3. 多了 ``<pulse>``：几个人在聊、安静了多久、有没有人问了话没人接。
           这些数字是代码算的，不让小模型自己数。
        """
        names = self.namebook()
        lines = self.render_scene(names)

        messages = [*self.history, self.current]

        self_lines = read_self_position(messages, now=self.now).render()
        if self_lines:
            lines.append("<me>")
            lines.extend(self_lines)
            lines.append("</me>")

        pulse_lines = read_pulse(messages, now=self.now, self_id=self.self_id).render()
        if pulse_lines:
            lines.append("<pulse>")
            lines.extend(pulse_lines)
            lines.append("</pulse>")

        interest_lines = render_interests(self.interest_hits)
        if interest_lines:
            lines.append("<interest>")
            lines.extend(interest_lines)
            lines.append("</interest>")

        history = self.render_history(names)
        if history:
            lines.append("<history>")
            lines.extend(history)
            lines.append("</history>")

        lines.append("<tail>")
        lines.extend(self.render_current(names))
        lines.append("</tail>")
        return "\n".join(lines)

    # -------- 媒体 --------
    def image_sources(self) -> dict[str, list[str] | dict[str, list[str]]]:
        """本轮工具可访问的图片。

        - ``current``：当前消息的图片 URL
        - ``quote``：被引用消息的图片 URL
        - ``history``：历史里带图的消息，``{message_id: [url]}``（按时间正序）

        只给工具解析用，不参与上下文渲染（长 URL 不进提示词）。
        """
        quote = self.current.quote
        history = {
            str(msg.message_id): msg.images
            for msg in self.history
            if msg.message_id and msg.images
        }
        return {
            "current": self.current.images,
            "quote": list(quote.images) if quote else [],
            "history": history,
        }

    # -------- 日志 --------
    def to_dict(self) -> dict:
        return {
            "chat_type": self.chat_type,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "self_id": self.self_id,
            "master_ids": list(self.master_ids),
            "now": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "note": self.note,
            "attention": self.attention_level,
            "attention_evidence": self.attention_evidence,
            "history": [m.to_dict() for m in self.history],
            "current": self.current.to_dict(),
        }

    def summary(self) -> str:
        """一行摘要，给日志/调试用。"""
        who = self.current.sender.label(self.namebook())
        text = clip(self.current.text or render_body(self.current, self.namebook()), 40)
        return f"{self.chat_type} {self.group_id or self.self_id} | 历史 {len(self.history)} 条 | 当前 {who}：{text}"
