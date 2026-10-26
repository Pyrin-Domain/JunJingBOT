"""本轮「动作检查」：按意图点名要调用的工具。

小模型（如 glm-4-flash）在长提示词 + 多工具下极易「只说不做」：嘴上答应
「我去找找看」，却从不调用工具。这里在命中动作意图时，往本轮用户消息尾部
追加一条**点名到工具**的命令式提醒。

同时放着女装照意图判定、@ 目标、图片来源解析这些纯辅助函数。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from messaging.models import ChatContext
from .output import _DOM_PREFIX


def _at_targets(event: Optional[dict]) -> list[str]:
    targets = []
    for seg in (event or {}).get("message") or []:
        if seg.get("type") != "at":
            continue
        qq = (seg.get("data") or {}).get("qq")
        if qq and str(qq) != "all":
            targets.append(str(qq))
    return targets


def _image_urls(event: Optional[dict]) -> list[str]:
    urls = []
    for seg in (event or {}).get("message") or []:
        if seg.get("type") != "image":
            continue
        url = (seg.get("data") or {}).get("url")
        if url:
            urls.append(url)
    return urls


# 工具里的图片来源别名：模型可能写中文或英文，统一成 current / quote / history
_IMAGE_SOURCE_ALIASES = {
    "current": "current", "now": "current", "self": "current",
    "当前": "current", "当前消息": "current", "这条": "current",
    "quote": "quote", "quoted": "quote", "reply": "quote", "replied": "quote",
    "引用": "quote", "被引用": "quote", "回复": "quote", "被回复": "quote",
    "history": "history", "hist": "history",
    "历史": "history", "历史消息": "history", "上一条": "history", "上一张": "history",
    "前面": "history", "之前": "history", "刚才": "history",
}


def _normalize_image_source(source: Any) -> str:
    return _IMAGE_SOURCE_ALIASES.get(str(source or "").strip().lower(), "current")


def _urls_for_source(
    sources: dict, source: str, message_id: str = ""
) -> list[str]:
    """按来源取图片 URL。history 支持指定 message_id，缺省取最近一条带图的消息。"""
    if source == "history":
        history = sources.get("history") or {}
        if not isinstance(history, dict):
            return []
        key = str(message_id or "").strip()
        if key:
            return list(history.get(key) or [])
        if not history:
            return []
        return list(history[next(reversed(history))] or [])
    return list(sources.get(source) or [])


def _replied_message_id(event: Optional[dict]) -> Optional[str]:
    for seg in (event or {}).get("message") or []:
        if seg.get("type") == "reply":
            rid = (seg.get("data") or {}).get("id")
            if rid:
                return str(rid)
    return None


# ── 女装照意图识别 ─────────────────────────────────────────────
# 提到「女装」≠ 想看照片。「你为什么羡慕女装」只是提问，不能触发 send_dress。
# 必须同时满足：①有求图动词 + 名词 ②不是否定句 ③不是问句/感想。
_DRESS_NOUN = r"(?:女装|腿照|白丝|黑丝|裙子照|裙子|腿)"
_DRESS_ASK = re.compile(
    # 动词在前：看看女装 / 发张女装照 / 来一张腿照 / 拍张白丝
    r"(?:发|发给|发张|发个|发一张|来|来个|来张|来一张|给|整|整张|拍|晒|秀|"
    r"看看|看一眼|看眼|瞅瞅|瞧|康康|欣赏|让我看)"
    rf"[^，。？！!?\n]{{0,6}}?{_DRESS_NOUN}"
    # 名词在前：女装照发我 / 腿照给我看看
    rf"|{_DRESS_NOUN}[^，。？！!?\n]{{0,4}}?(?:发我|发给我|给我看|来一张|来张|看看|瞅瞅)"
)
# 问句 / 感想：在讨论女装本身，不是要图
_DRESS_QUESTION = re.compile(
    r"为什么|为啥|为何|怎么|怎样|如何|什么意思|什么感觉|"
    r"是不是|是否|羡慕|嫉妒|觉得|认为|评价|感想|谈到|提到|说起|关于"
)
# 否定：不想看 / 别发 / 不要女装照
_DRESS_NEGATION = re.compile(
    rf"(?:不|别|没|甭|勿|无需|不用)[^，。？！!?\n]{{0,3}}{_DRESS_NOUN}"
)


def _wants_dress_photo(text: str) -> bool:
    """这条消息是不是在**要**女装照（而不是在讨论女装）。"""
    if not text or not _DRESS_ASK.search(text):
        return False
    if _DRESS_NEGATION.search(text):
        return False
    if _DRESS_QUESTION.search(text):
        return False
    return True


_DRESS_TOPIC_RE = re.compile(r"女装|腿照|白丝|黑丝|裙子照")


def _dress_is_just_talk(text: str) -> bool:
    """在聊女装、但并不是要照片（提问 / 感慨 / 明确拒绝 / 只是提到）。"""
    if not text or not _DRESS_TOPIC_RE.search(text):
        return False
    return not _wants_dress_photo(text)


# ── 表情包：按情绪点名提示 ─────────────────────────────────────
# 小模型（如 glm-4-flash）在长提示词下很少主动写 <meme>：它把表情包当成
# 「可选彩蛋」，默认只回文字，偶尔还会写成 `[表情:名字]` 这种普通文本。
# 这里在情绪明显时给一条点名提示，并把候选名字先拿到表情库里核对，
# 避免提示一个不存在的名字。提示只是「建议」，不会替模型做决定。
_MEME_REQUEST_RE = re.compile(
    r"(?:发|来|整|给|要|看看|来个|发个|发张)[^，。？！!?\n]{0,8}"
    r"(?:表情包|表情|贴纸|meme)",
    re.I,
)

# (情绪标签, 触发正则, 优先候选名字)；候选按顺序取，库里没有就跳过
_MEME_EMOTIONS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    (
        "撒娇",
        re.compile(r"撒娇|贴贴|抱我|要抱|抱抱|亲亲|摸摸头|蹭蹭|抱一抱"),
        ("抱抱", "要抱", "亲亲", "摸头", "嘬嘬"),
    ),
    (
        "安慰",
        re.compile(r"呜呜|哭|难过|委屈|心疼|安慰|好累|累了|辛苦|难受|不开心|低落|摸摸"),
        ("摸头", "抱抱", "抱哭", "亲亲", "要抱", "妈咪抱抱"),
    ),
    (
        "开心",
        re.compile(r"哈哈|笑死|好笑|好耶|太好了|开心|高兴|快乐|乐死|绷不住"),
        ("笑哭", "赞", "吐舌", "可爱打钱", "嘬嘬"),
    ),
    (
        "无语",
        re.compile(r"无语|离谱|逆天|服了|啊这|汗|太离谱|什么鬼"),
        ("逆天", "问号", "捂脸", "6w9", "对的不对"),
    ),
    (
        "震惊",
        re.compile(r"震惊|卧槽|真的假的|不会吧|吓|惊了|天哪|我去"),
        ("震惊", "问号", "逆天", "哇哇哇哇"),
    ),
    (
        "害羞",
        re.compile(r"害羞|脸红|别说了|讨厌|不好意思|羞涩|捂脸|羞|骗人|撒谎|才没有|眼睛都红|被发现"),
        ("捂脸", "吐舌", "亲亲", "嘬嘬"),
    ),
    (
        "疑惑",
        re.compile(r"是不是|对不对|好不好|行不行|真的吗|你说呢"),
        ("问号", "对的不对", "6w9", "智慧"),
    ),
    (
        "生气",
        re.compile(r"生气|哼|不理你|烦|气死|可恶"),
        ("不乖", "急了", "坏东西", "给爷爬", "对的不对"),
    ),
    (
        "得意",
        re.compile(r"厉害|好棒|夸|得意|骄傲|聪明|牛|强"),
        ("聪明小马", "赞", "顶你", "智慧"),
    ),
    (
        "干饭",
        re.compile(r"饿|吃|干饭|饭|好吃|馋|奶茶"),
        ("吃吃吃", "先吃饭", "冰淇淋", "喝奶茶", "美味角马"),
    ),
    (
        "游戏",
        re.compile(r"游戏|开黑|上号|来不来|带我|打游戏|吃鸡|王者|联盟"),
        ("我来c", "小马出击", "吃兵", "辅助宣言"),
    ),
    (
        "摸鱼",
        re.compile(r"摸鱼|摆烂|不想动|困|睡|躺|懒"),
        ("似了", "没我事", "摸肚子", "喝晕晕"),
    ),
)


def _available_meme_names(
    candidates: tuple[str, ...],
    keywords: tuple[str, ...] = (),
    limit: int = 4,
) -> list[str]:
    """候选名字里真实存在的几条；不够时再按描述关键词补。"""
    try:
        from messaging.meme import get_library, is_enabled

        if not is_enabled():
            return []
        library = get_library()
    except Exception:  # noqa: BLE001 - 表情包坏了不能影响提示
        return []

    names: list[str] = []
    for name in candidates:
        try:
            entry = library.get(name)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is not None and entry.name not in names:
            names.append(entry.name)
            if len(names) >= limit:
                return names

    for keyword in keywords:
        try:
            hits = library.search(keyword, limit=limit)
        except Exception:  # noqa: BLE001
            hits = []
        for entry in hits:
            if entry.name not in names:
                names.append(entry.name)
                if len(names) >= limit:
                    return names
    return names


def build_meme_hint(user_message: str) -> str:
    """情绪明显 / 对方点名要表情包时，给模型一条可执行的表情包提示。

    没有命中情绪、表情包被关掉、或库里找不到候选名字时返回空串。
    """
    text = str(user_message or "")
    if not text:
        return ""

    try:
        from messaging.meme import get_library, is_enabled

        if not is_enabled() or not get_library():
            return ""
    except Exception:  # noqa: BLE001 - 表情包坏了不能连累主流程
        return ""

    if _MEME_REQUEST_RE.search(text):
        return (
            "- 对方明确要表情包：必须真的发一个，用 "
            "`<message>短句<meme>名字</meme></message>`；名字从目录里照抄，"
            "不要只回文字，也不要用 `[表情:名字]` 代替。"
        )

    for label, pattern, candidates in _MEME_EMOTIONS:
        if not pattern.search(text):
            continue
        names = _available_meme_names(candidates, keywords=(label,))
        if not names:
            continue
        return (
            f"- 这条消息情绪明显（{label}）。回复时带一个贴切的表情包："
            f"`<message>短句<meme>名字</meme></message>`；"
            f"可选：{'、'.join(names)}。别只发纯文字，也别写 `[表情:名字]`。"
        )
    return ""


def build_action_hint(
    user_message: str,
    event: Optional[dict] = None,
    is_dom: bool = False,
    context: Optional["ChatContext"] = None,
) -> str:
    """按本轮意图生成点名的「动作检查」；没有动作意图时返回空串。

    只有命中规则才注入，普通闲聊不注入（省 token、也避免误触发工具）。
    传了 ``context`` 时，图片来源（当前消息 / 被引用消息）直接从结构体里取，
    比只看 event 更准（引用消息的图片是采集层异步补全的）。
    """
    text = user_message or ""
    if not text:
        return ""

    event = event or {}
    group_id = event.get("group_id")
    sender_id = event.get("user_id")
    message_id = event.get("message_id")
    hints: list[str] = []

    def _dress_call() -> str:
        """生成 send_dress 的调用示例（群聊 / 私聊）。"""
        if group_id:
            return f'send_dress(group_id={group_id})'
        return f'send_dress(message_type="private", user_id={sender_id})'

    # 1. 女装照 / 腿照：只在「明确要图」时触发，讨论/提问女装不触发
    if _wants_dress_photo(text):
        if is_dom and (group_id or sender_id):
            hints.append(
                f"发送者是主人，他想看女装照。你必须**先进行原生工具调用** "
                f"{_dress_call()} 把照片真的发出去，等工具返回后再输出 "
                f"<status>/\u003cthink>/<output>。只回复文字而不调用工具属于失职。"
            )
        elif not is_dom:
            hints.append(
                "对方不是主人却在要女装照：**不要**调用 send_dress，用 <message> 俏皮地拒绝他。"
            )
    # 1.5 笼统地要照片：君景唯一的图源就是女装照
    elif re.search(r"发张|发一张|来一张|来个图|发个图|看看照片", text) and (group_id or sender_id):
        if is_dom:
            hints.append(
                f"主人要你发照片。你手上能发的就是女装照：你必须**先进行原生工具调用** "
                f"{_dress_call()} 把照片发出去，等工具返回后再输出 "
                f"<status>/\u003cthink>/<output>。只回复文字而不调用工具属于失职。"
            )
        else:
            hints.append(
                "对方不是主人却在要照片：**不要**调用 send_dress，用 <message> 俏皮地拒绝，"
                "不要嘴上答应“去准备一下”却什么都不做。"
            )

    # 2. 戳一戳
    if re.search(r"戳一戳|戳一下|戳戳|戳我|poke", text, re.I):
        targets = _at_targets(event)
        target = targets[0] if targets else sender_id
        if target and group_id:
            hints.append(
                f"有人要戳一戳：你必须**先进行原生工具调用** "
                f"poke(user_id={target}, group_id={group_id})，然后再输出 <output>。"
            )
        elif target:
            hints.append(
                f"有人要戳一戳：你必须**先进行原生工具调用** poke(user_id={target})，然后再输出 <output>。"
            )

    # 3. 撤回消息
    if re.search(r"撤回|撤了|撤一下|删掉那条|把上一条", text):
        target_id = _replied_message_id(event) or "要撤回的那条消息ID"
        hints.append(
            f"对方要求撤回消息：你必须**先进行原生工具调用** recall_msg(message_id={target_id})，"
            f"然后再输出 <output>。"
        )

    # 4. 禁言：绝大多数是玩笑，重点在「别真的禁言」
    if re.search(r"禁言|关小黑屋", text):
        hints.append(
            "有人提到禁言：这几乎一定是玩笑。**严禁**真的调用 set_ban；"
            "用语言吐槽，或者 emoji_like / poke 回应即可。"
        )

    # 5. 图片文字识别（URL 由服务端按 source/index 解析，模型不需要复述长 URL）
    sources = (
        context.image_sources()
        if context is not None
        else {"current": _image_urls(event), "quote": [], "history": {}}
    )
    current_urls = sources.get("current") or []
    quote_urls = sources.get("quote") or []
    history_urls = sources.get("history") or {}
    # 只有出现「上一条 / 前面那张」这类回指时才去历史里找图，
    # 避免普通聊到“图片”就把十楼前的图 OCR 一遍。
    wants_history = bool(history_urls) and bool(
        re.search(r"上一条|上一张|前面那|之前那|刚才那|刚才的图|那个图|历史.{0,4}图", text)
    )
    if (current_urls or quote_urls or wants_history) and re.search(
        r"写了什么|什么字|识别|这图|图片|图上|截图|ocr", text, re.I
    ):
        calls: list[str] = []
        if current_urls:
            calls.append(
                "ocr_img()"
                if len(current_urls) == 1
                else f"ocr_img(index=0)（当前消息共 {len(current_urls)} 张，依次用 index=0..{len(current_urls) - 1}）"
            )
        if quote_urls:
            calls.append(
                'ocr_img(source="quote")'
                if len(quote_urls) == 1
                else f'ocr_img(source="quote", index=0)（被引用消息共 {len(quote_urls)} 张，依次用 index=0..{len(quote_urls) - 1}）'
            )
        if wants_history:
            calls.append(
                'ocr_img(source="history")（默认取历史上最近一张带图的消息）'
            )
        hints.append(
            "对方想知道图片内容：你必须**先进行原生工具调用** "
            + "；".join(calls)
            + "，等工具返回后再输出。"
        )

    # 6. 查历史
    if re.search(r"历史|刚才说|之前说|上一条|前面说|记录|统计", text):
        if group_id:
            hints.append(
                f"对方要查聊天记录：请**先进行原生工具调用** "
                f"get_msg_history(group_id={group_id}, message_id={message_id}, count=20)，"
                f"再依据结果回答。"
            )

    if not hints:
        return ""
    return "\n".join(f"- {h}" for h in hints)


__all__ = [
    "_DOM_PREFIX",
    "_at_targets",
    "_dress_is_just_talk",
    "_image_urls",
    "_normalize_image_source",
    "_replied_message_id",
    "_urls_for_source",
    "_wants_dress_photo",
    "build_action_hint",
    "build_meme_hint",
]
