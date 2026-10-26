"""上下文协议：事件 -> 结构体 -> 紧凑文本。

重点覆盖「别再出现的旧问题」：
- 图片 URL 不再内联（省 token，OCR 走工具参数 `source`/`index`）；
- 引用只留一行摘要，不递归展开；
- 历史里的转发只留条数；
- 当前消息不会在历史里重复出现；
- 超预算时从最旧的开始丢，并明确标注省略了多少条。
"""

from datetime import datetime

from messaging.models import (
    ChatContext,
    ChatMessage,
    Quote,
    Segment,
    Sender,
    is_legacy_context_note,
    parse_event,
    render_body,
    render_message,
)

NOW = datetime(2026, 10, 18, 21, 4, 7)
SELF_ID = "10002"
MASTER_ID = "10001"
GROUP = "20001"


def _event(mid, qq, name, segments, ts=NOW.timestamp(), **extra):
    event = {
        "message_id": mid,
        "time": ts,
        "user_id": qq,
        "sender": {"user_id": qq, "nickname": name},
        "message": segments,
    }
    event.update(extra)
    return event


def _text(value):
    return {"type": "text", "data": {"text": value}}


def _at(qq):
    return {"type": "at", "data": {"qq": qq}}


def _image(url="https://multimedia.nt.qq.com.cn/download?fileid=very-long-signature"):
    return {"type": "image", "data": {"url": url, "summary": "[动画表情]"}}


def _msg(mid, qq, name, text, ts=NOW, is_self=False, is_master=False, quote=None, forward_total=0):
    return ChatMessage(
        message_id=mid,
        time=ts,
        sender=Sender(qq=qq, name=name, is_self=is_self, is_master=is_master),
        segments=[Segment("text", text=text)],
        quote=quote,
        forward_total=forward_total,
    )


# ── 解析 ────────────────────────────────────────────────────
def test_parse_marks_self_and_master():
    me = parse_event(_event(1, SELF_ID, "君景", [_text("嗯")]), self_id=SELF_ID, master_ids=[MASTER_ID])
    other = parse_event(_event(2, MASTER_ID, "Pyrin", [_text("在吗")]), self_id=SELF_ID, master_ids=[MASTER_ID])
    assert me.sender.is_self and not me.sender.is_master
    assert other.sender.is_master and not other.sender.is_self


def test_parse_keeps_segment_order_and_flags():
    msg = parse_event(
        _event(3, MASTER_ID, "Pyrin", [_at(SELF_ID), _text(" 看看"), _image(), _text("这图")]),
        self_id=SELF_ID,
        master_ids=[MASTER_ID],
        now=NOW,
    )
    assert [s.type for s in msg.segments] == ["at", "text", "image", "text"]
    assert msg.mentions == [SELF_ID]
    assert len(msg.images) == 1
    assert msg.text == "看看这图"


def test_parse_drops_reply_segment_and_falls_back_to_raw():
    event = _event(4, MASTER_ID, "Pyrin", [{"type": "reply", "data": {"id": 99}}])
    event["raw_message"] = "纯文本兜底"
    msg = parse_event(event, self_id=SELF_ID, master_ids=[MASTER_ID], now=NOW)
    assert [s.type for s in msg.segments] == ["text"]
    assert msg.text == "纯文本兜底"


# ── 渲染 ────────────────────────────────────────────────────
def test_renders_time_id_sender_and_master_mark():
    msg = _msg("1059815614", MASTER_ID, "Pyrin Domain", "你好", is_master=True)
    line = render_message(msg, now=NOW)
    assert line == "21:04 [1059815614] Pyrin Domain(10001,主人)：你好"


def test_mention_self_renders_as_at_me():
    msg = parse_event(
        _event(5, MASTER_ID, "Pyrin", [_at(SELF_ID), _text(" 好")]),
        self_id=SELF_ID,
        master_ids=[MASTER_ID],
        now=NOW,
    )
    ctx = ChatContext(self_id=SELF_ID, now=NOW, current=msg)
    assert "@我" in ctx.render()


def test_image_is_placeholder_not_url():
    msg = parse_event(
        _event(6, MASTER_ID, "Pyrin", [_image()]),
        self_id=SELF_ID,
        master_ids=[MASTER_ID],
        now=NOW,
    )
    line = render_message(msg, now=NOW)
    assert line.endswith("：[图片]")
    assert "multimedia" not in line  # URL 长且只有 OCR 用得上，不能塞进正文


def test_quote_is_one_line_summary():
    quote = Quote(
        message_id="1059815600",
        sender=Sender(qq=MASTER_ID, name="Pyrin", is_master=True),
        summary="看看腿",
    )
    msg = _msg("1059815617", MASTER_ID, "Pyrin", "今天吃什么", quote=quote, is_master=True)
    line = render_message(msg, now=NOW)
    assert line.endswith("　↳[1059815600] Pyrin(10001,主人)：看看腿")
    assert "被回复的内容如下" not in line


def test_forward_shows_total_inline():
    msg = _msg("1059815620", MASTER_ID, "Pyrin", "", forward_total=36)
    line = render_message(msg, now=NOW)
    assert "【转发聊天记录 36 条】" in line


def test_body_media_placeholders():
    msg = ChatMessage(
        segments=[
            Segment("text", text="看图"),
            Segment("image"),
            Segment("face"),
            Segment("file", text="a.pdf"),
        ]
    )
    assert render_body(msg) == "看图[图片][表情][文件 a.pdf]"


# ── 场景与预算 ──────────────────────────────────────────────
def _ctx(history=None, current=None, **kwargs):
    return ChatContext(
        chat_type="group",
        group_id="20001",
        group_name="君景的独角兽馆",
        self_id=SELF_ID,
        self_name="君景",
        master_ids=[MASTER_ID],
        now=NOW,
        history=history or [],
        current=current or _msg("900", MASTER_ID, "Pyrin", "在吗", is_master=True),
        **kwargs,
    )


def test_scene_has_group_self_master_and_time():
    text = _ctx().render()
    assert "<scene>" in text and "</scene>" in text
    assert "群聊 群号=20001 群名=君景的独角兽馆" in text
    assert "我=君景/10002" in text
    assert "主人=Pyrin/10001" in text  # 名字从历史/当前消息的发送者里补
    assert "现在=2026-10-18 21:04" in text


def test_current_not_duplicated_in_history():
    current = _msg("900", MASTER_ID, "Pyrin", "在吗", is_master=True)
    history = [_msg("899", MASTER_ID, "Pyrin", "上一条", is_master=True)]
    text = _ctx(history=history, current=current).render()
    assert text.count("[900]") == 1
    assert "[899]" in text


def test_history_budget_drops_oldest_and_marks_omitted():
    history = [_msg(str(100 + i), MASTER_ID, "Pyrin", "很长的一句话" * 20) for i in range(10)]
    text = _ctx(history=history, max_history_chars=300, max_history_messages=10).render()
    assert "…（更早" in text
    assert "[109]" in text          # 新的保留
    assert "[100]" not in text      # 最旧的被裁掉


def test_history_keeps_newest_even_when_over_budget():
    history = [_msg("1", MASTER_ID, "Pyrin", "超长" * 500)]
    text = _ctx(history=history, max_history_chars=50).render()
    assert "[1]" in text


def test_history_message_body_is_truncated():
    history = [_msg("2", MASTER_ID, "Pyrin", "字" * 500)]
    line = render_message(history[0], now=NOW)
    assert "(+200字)" in line


def test_legacy_template_note_is_ignored():
    legacy = (
        "当前是群聊，群号 {group_id}，发消息的用户 QQ 号是 {user_id}，"
        "消息 ID 是 {message_id},AT的对象的QQ号是{ated_user},"
        "你的QQ号是{self_id},主人的QQ号是{master_id}"
    )
    assert is_legacy_context_note(legacy)
    assert "备注=" not in _ctx(note=legacy).render()

    assert not is_legacy_context_note("这是大连理工的校园群")
    assert "备注=这是大连理工的校园群" in _ctx(note="这是大连理工的校园群").render()


def test_context_render_order_scene_history_current():
    history = [_msg("899", MASTER_ID, "Pyrin", "上一条", is_master=True)]
    text = _ctx(history=history).render()
    assert text.index("<scene>") < text.index("<history>") < text.index("<current>")
    assert text.rstrip().endswith("</current>")


def test_to_dict_is_serialisable():
    data = _ctx(history=[_msg("899", MASTER_ID, "Pyrin", "上一条")]).to_dict()
    assert data["group_id"] == "20001"
    assert data["history"][0]["sender"]["qq"] == MASTER_ID
    assert data["current"]["message_id"] == "900"


def test_build_chat_context_ignores_legacy_note_before_formatting():
    """旧模板是带占位符的，必须先判 legacy 再 format，否则会被当成自定义备注渲染出来。"""
    import asyncio

    from messaging.processor import MessageProcessor

    class _MP(MessageProcessor):
        async def ensure_connected(self):
            return None

    mp = _MP(None, None)
    mp.user_id = SELF_ID
    legacy = (
        "当前是群聊，群号 {group_id}，发消息的用户 QQ 号是 {user_id}，"
        "消息 ID 是 {message_id},AT的对象的QQ号是{ated_user},"
        "你的QQ号是{self_id},主人的QQ号是{master_id}"
    )
    ctx = asyncio.run(
        mp.build_chat_context(
            event=_event(1, MASTER_ID, "Pyrin", [_text("在吗")]),
            history=[],
            note=legacy,
            note_values={"group_id": GROUP, "user_id": MASTER_ID},
            master_ids=[MASTER_ID],
            now=NOW,
        )
    )
    rendered = ctx.render()
    assert "备注=" not in rendered
    assert "消息 ID 是" not in rendered

    ctx2 = asyncio.run(
        mp.build_chat_context(
            event=_event(1, MASTER_ID, "Pyrin", [_text("在吗")]),
            history=[],
            note="这是 {group_id} 的校园群",
            note_values={"group_id": GROUP},
            master_ids=[MASTER_ID],
            now=NOW,
        )
    )
    assert "备注=这是 20001 的校园群" in ctx2.render()


# ── 图片：引用消息也要留给工具 ──────────────────────────────
def test_quote_keeps_images_for_ocr():
    """被引用消息的图片 URL 要跟着 Quote 留下来，OCR 工具才能识别引用的图。"""
    import asyncio

    from messaging.processor import MessageProcessor

    class _MP(MessageProcessor):
        async def ensure_connected(self):
            return None

        async def _get_message_event(self, message_id, cache=None):
            return _event(99, MASTER_ID, "Pyrin", [_image("https://x/quoted.png")])

    async def run():
        mp = _MP(None, None)
        mp.user_id = SELF_ID
        current = _event(
            100,
            MASTER_ID,
            "Pyrin",
            [{"type": "reply", "data": {"id": 99}}, _text("这图里写了什么")],
        )
        return await mp.collect_message(current, master_ids=[MASTER_ID], now=NOW)

    msg = asyncio.run(run())
    assert msg.quote is not None
    assert msg.quote.images == ["https://x/quoted.png"]
    # 图片 URL 仍然不进渲染文本，只在 image_sources 里给工具用
    assert "https://x/quoted.png" not in _ctx(current=msg).render()
    assert _ctx(current=msg).image_sources() == {
        "current": [],
        "quote": ["https://x/quoted.png"],
        "history": {},
    }


def test_image_sources_indexes_history_by_message_id():
    """历史里的图按 message_id 建索引，供 OCR 定位「上一条图」。"""
    history = [
        ChatMessage(message_id="10", segments=[Segment("image", url="https://x/old.png")]),
        ChatMessage(message_id="11", segments=[Segment("text", text="没图")]),
        ChatMessage(message_id="12", segments=[Segment("image", url="https://x/new.png")]),
    ]
    assert _ctx(history=history).image_sources()["history"] == {
        "10": ["https://x/old.png"],
        "12": ["https://x/new.png"],
    }
