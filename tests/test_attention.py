"""注意力判定：这条消息是在叫它、在说它，还是跟它无关。

重点保护两类曾经会「石沉大海」的消息：
    - 群里喊名字但没 @（「君景呢」「君景在吗」）→ 必须进正式回复
    - 只是背后提到（「君景好可爱」）→ 允许插话器自己决定，不强制回复
"""
from bot.services.interject import InterjectService
from messaging.attention import (
    ABOUT_ME,
    AMBIENT,
    SUMMON,
    detect_attention,
    message_text,
    mentions,
)
from messaging.models import ChatContext, ChatMessage, Segment, Sender

SELF_ID = "10002"


def _group(text: str = "", *, at: str | None = None, raw: str = "") -> dict:
    segments = []
    if at:
        segments.append({"type": "at", "data": {"qq": at}})
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    event = {
        "message_type": "group",
        "group_id": "1",
        "user_id": "2",
        "message": segments,
    }
    if raw:
        event["raw_message"] = raw
    return event


def _level(text: str, **kwargs) -> str:
    return detect_attention(chat_type="group", text=text, **kwargs).level


# ── summon：当面叫它 ──────────────────────────────────────
def test_private_chat_is_summon():
    assert detect_attention(chat_type="private", text="在吗").level == SUMMON


def test_at_me_is_summon():
    assert detect_attention(chat_type="group", text="你好", mentions_me=True).level == SUMMON


def test_quote_me_is_summon():
    assert detect_attention(chat_type="group", text="然后呢", replies_to_me=True).level == SUMMON


def test_bare_name_is_summon():
    """只喊名字 + 语气词：QQ 群里最常见的召唤，以前完全识别不到。"""
    for text in ("君景", "君景呢", "君景！", "君景？？", "君景酱"):
        assert _level(text) == SUMMON, text


def test_name_plus_call_word_is_summon():
    for text in (
        "君景在吗",
        "君景在不在",
        "君景你觉得呢",
        "君景帮我看下这个",
        "君景出来",
        "君景在干嘛呀",
    ):
        assert _level(text) == SUMMON, text


def test_name_plus_second_person_is_summon():
    """名字打头再「你…」就是在对它说话。

    真实漏判：「君景你最近在忙啥呢」以前判 about_me → 不进正式回复，
    插话器又判 silent，整句石沉大海。
    """
    for text in (
        "君景你最近在忙啥呢",
        "君景你怎么看",
        "君景你们群里好安静",
        "君景你是不是又熬夜了",
        "君景怎么不说话",
    ):
        assert _level(text) == SUMMON, text


# ── about_me：在说它，没点名 ──────────────────────────────
def test_praising_behind_back_is_not_summon():
    """背后夸不该逼它回话，但要让它知道有人在说自己。"""
    for text in ("君景好可爱", "君景在群里好安静", "君景这个机器人有点东西"):
        assert _level(text) == ABOUT_ME, text


def test_talking_about_robot_is_about_me():
    assert _level("这个机器人是自动回复吗") == ABOUT_ME
    assert _level("bot 好像在线") == ABOUT_ME


# ── ambient：跟它无关 ────────────────────────────────────
def test_plain_chat_is_ambient():
    for text in ("今天吃什么", "有人打游戏吗", "睡了睡了"):
        assert _level(text) == AMBIENT, text


# ── 别名配置 ────────────────────────────────────────────
def test_custom_alias_is_honored():
    assert _level("小景在吗", aliases=["小景"]) == SUMMON
    assert _level("小景今天没来", aliases=["小景"]) == ABOUT_ME


def test_event_helpers():
    event = _group("君景在吗", at=SELF_ID)
    assert message_text(event) == "君景在吗"
    assert mentions(event, SELF_ID) is True
    assert mentions(event, "999") is False


def test_message_text_strips_cq_code():
    assert message_text({"raw_message": "[CQ:at,qq=1] 君景在吗"}) == "君景在吗"


# ── ChatContext：注意力影响「必须回复」 ────────────────────
def _ctx(text: str = "今天吃什么", **kwargs) -> ChatContext:
    message = ChatMessage(
        message_id="1",
        sender=Sender(qq="2", name="路人"),
        segments=[Segment("text", text=text)],
    )
    return ChatContext(
        chat_type="group", group_id="1", self_id=SELF_ID, current=message, **kwargs
    )


def test_context_attention_drives_directed_at_me():
    ctx = _ctx("君景呢")
    assert ctx.directed_at_me is False          # 没被显式判定过
    ctx.attention = SUMMON
    assert ctx.directed_at_me is True
    assert ctx.attention_level == SUMMON


def test_context_attention_level_falls_back_to_ambient():
    ctx = _ctx("今天吃什么")
    assert ctx.attention_level == AMBIENT


# ── 插话计数：被叫的消息不算「群里随便聊起来的一段」 ────────
class _MP:
    user_id = SELF_ID


def _service() -> InterjectService:
    return InterjectService(mp=_MP(), settings={"interject": {"enable": True}})


def test_note_message_skips_summon():
    service = _service()
    assert service.note_message(_group("君景呢")) is None
    assert service.note_message(_group("你好", at=SELF_ID)) is None
    assert service.note_message(_group("今天吃什么")) == 1
    assert service.note_message(_group("有人打游戏吗")) == 2
