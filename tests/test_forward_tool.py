"""合并转发 / 聊天记录卡片解析工具（``read_forward``）与 OCR 联动。

现场（logs/agent/2026-11-02.json）：主人发了一张转发卡片，十几秒后问
「总结一下这个聊天记录」，模型回了「那条转发我这边只有个壳子 / 内容读不出来呀」
—— 一次工具都没调。再下一句「调用识图读这里面的图片」，模型拿 OCR 去读历史里
另一张不相干的图，结论还是「壳子里一张图都没有」。

这里覆盖三件事：

1. 工具能把卡片展开成可读文本（两种后端的 id 形态都要认）；
2. 卡片里的图片会登记成 ``source="forward"``，``ocr_img`` 随后能读到；
3. 上下文里没有卡片时，工具给的是「读不出来」的**可读**提示，不编内容。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.hints import build_action_hint
from agent.runtime import QQBotAgent
from agent.tools.registry import (
    _current_context_var,
    _current_images_var,
    reset_turn_state,
)
from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

NOW = "2026-11-02 10:30:00"
CARD_ID = "1462453708"
FORWARD_ID = "ZeOIi42XtoBHE9EG7cDAaSvswRgJe4k8SbhgPPULt9Nexa82MD41WLNzwnY1/808"
IMAGE_URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=EhS2LCtz"


@pytest.fixture(autouse=True)
def _clean_turn_state():
    """每个用例都从干净的「轮次状态」开始。

    ContextVar 的默认值是**进程内共享**的容器（原因见 registry 里的注释），
    不清一下的话，上一个用例登记的转发图片 / 工具缓存会漏进下一个。
    """
    reset_turn_state()
    _current_images_var.set({})
    _current_context_var.set(None)
    yield
    reset_turn_state()
    _current_images_var.set({})
    _current_context_var.set(None)


# ══════════════════════════════════════════════════════════
# 假实现
# ══════════════════════════════════════════════════════════
def _card_event(message_id: str = CARD_ID) -> dict:
    """一张转发卡牌的原始事件（SnowLuma 形态：message 段带 forward.id）。"""
    return {
        "message_id": message_id,
        "time": 1762150000,
        "user_id": "3122793026",
        "sender": {"user_id": "3122793026", "nickname": "我"},
        "message": [
            {"type": "forward", "data": {"id": FORWARD_ID}},
        ],
        "raw_message": f"[CQ:forward,id={FORWARD_ID}]",
    }


def _leaf(text: str = "", url: str = "") -> dict:
    segs = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    if url:
        segs.append({"type": "image", "data": {"url": url, "summary": "[图片]", "sub_type": 0}})
    return {
        "message_id": "sub-1",
        "time": 1762150010,
        "user_id": "1094950020",
        "sender": {"user_id": "1094950020", "nickname": "高光的QC", "card": ""},
        "message": segs,
        "raw_message": text,
    }


class _FakeKernel:
    """只认 ``get_forward_msg`` 的假内核。"""

    def __init__(self, messages=None, accept_ids=None):
        self.messages = messages if messages is not None else []
        self.accept_ids = set(accept_ids or [])
        self.calls: list[str] = []
        self.capabilities = type("Caps", (), {"forward_inner_id_fetchable": False})()

    async def call(self, action, params=None):
        if action != "get_forward_msg":
            return type("R", (), {"ok": False, "messages": [], "error": "unsupported"})()
        mid = str((params or {}).get("message_id") or "")
        self.calls.append(mid)
        if self.accept_ids and mid not in self.accept_ids:
            return type(
                "R",
                (),
                {"ok": False, "messages": [], "error": "消息已过期或者为内层消息"},
            )()
        return type(
            "R",
            (),
            {"ok": True, "messages": list(self.messages), "error": ""},
        )()


class _FakeAPI:
    def __init__(self, kernel, event=None):
        self.kernel = kernel
        self.event = event
        self.forward_calls: list[dict] = []

    async def get_message(self, message_id):
        if self.event is None:
            return {"status": "failed", "retcode": 100, "data": None}
        return {"status": "ok", "retcode": 0, "data": self.event}

    async def get_forward_msg(self, params):
        self.forward_calls.append(params)
        res = await self.kernel.call("get_forward_msg", params)
        if not res.ok:
            return {"status": "failed", "retcode": 200, "message": res.error}
        return {"status": "ok", "retcode": 0, "data": {"messages": res.messages}}


class _FakeExtension:
    def __init__(self):
        self.calls: list[str] = []

    async def napcat_ocr(self, img_url: str, re_ocr: bool = False) -> dict:
        self.calls.append(img_url)
        return {"text": f"OCR:{img_url}", "raw": None}


def _tool_agent(api=None, extension=None):
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.napcat_api = api
    agent.extension = extension
    agent.mp = None
    agent.rw_tool = None
    agent.tools = agent._build_tools()
    return agent


def _tool(agent, name):
    return next(t for t in agent.tools if t.name == name)


def _ctx_with_history_card() -> ChatContext:
    """卡片已经沉到历史里（主人隔几分钟才回头问的那种情形）。"""
    return ChatContext(
        chat_type="group",
        group_id="1079845768",
        self_id="3122793026",
        history=[
            ChatMessage(
                message_id=CARD_ID,
                sender=Sender(qq="3122793026", name="我", is_self=True),
                segments=[Segment("text", text=f"[CQ:forward,id={FORWARD_ID}]")],
                forward_total=5,
            ),
            ChatMessage(
                message_id="1462453709",
                sender=Sender(qq="1013098110", name="Pyrin", is_master=True),
                segments=[Segment("text", text="总结一下这个聊天记录")],
            ),
        ],
        current=ChatMessage(
            message_id="1462453710",
            sender=Sender(qq="1013098110", name="Pyrin", is_master=True),
            segments=[Segment("text", text="总结一下这个聊天记录")],
        ),
    )


# ══════════════════════════════════════════════════════════
# 工具本体
# ══════════════════════════════════════════════════════════
def test_read_forward_expands_card_from_history():
    """卡片沉在历史里时，工具自动定位并把内容读出来。"""
    kernel = _FakeKernel(messages=[_leaf("早啊"), _leaf("睡了吗", IMAGE_URL)])
    api = _FakeAPI(kernel, event=_card_event())
    agent = _tool_agent(api=api)
    read_forward = _tool(agent, "read_forward")

    ctx = _ctx_with_history_card()

    async def run():
        _current_context_var.set(ctx)
        _current_images_var.set(ctx.image_sources())
        return await read_forward.ainvoke({})

    text = asyncio.run(run())
    assert "合并转发聊天记录" in text
    assert "高光的QC" in text
    assert "睡了吗" in text
    # 优先用卡片事件里的 forward.id
    assert kernel.calls[0] == FORWARD_ID


def test_read_forward_falls_back_to_card_message_id():
    """forward.id 取不到时，退到卡片自身的 message id 再试一次。"""
    kernel = _FakeKernel(messages=[_leaf("兜底成功")], accept_ids=[CARD_ID])
    api = _FakeAPI(kernel, event=_card_event())
    agent = _tool_agent(api=api)
    read_forward = _tool(agent, "read_forward")

    ctx = _ctx_with_history_card()

    async def run():
        _current_context_var.set(ctx)
        _current_images_var.set(ctx.image_sources())
        return await read_forward.ainvoke({})

    text = asyncio.run(run())
    assert "兜底成功" in text
    assert CARD_ID in kernel.calls


def test_read_forward_registers_images_for_ocr():
    """卡片里的图登记成 source="forward"，随后 ocr_img 能直接读到。"""
    kernel = _FakeKernel(messages=[_leaf("看图", IMAGE_URL)])
    api = _FakeAPI(kernel, event=_card_event())
    extension = _FakeExtension()
    agent = _tool_agent(api=api, extension=extension)
    read_forward = _tool(agent, "read_forward")
    ocr_img = _tool(agent, "ocr_img")

    ctx = _ctx_with_history_card()

    async def run():
        _current_context_var.set(ctx)
        _current_images_var.set(ctx.image_sources())
        expanded = await read_forward.ainvoke({})
        read = await ocr_img.ainvoke({"source": "forward", "index": 0})
        return expanded, read

    expanded, read = asyncio.run(run())
    assert "1 张图片" in expanded
    assert read == f"OCR:{IMAGE_URL}"
    assert extension.calls == [IMAGE_URL]


def test_read_forward_without_card_returns_readable_hint():
    """上下文里没有卡片时给可读提示，不编内容、也不静默。"""
    kernel = _FakeKernel(messages=[])
    api = _FakeAPI(kernel, event=None)
    agent = _tool_agent(api=api)
    read_forward = _tool(agent, "read_forward")

    ctx = ChatContext(
        current=ChatMessage(
            message_id="1",
            sender=Sender(qq="1013098110", name="Pyrin"),
            segments=[Segment("text", text="总结一下这个聊天记录")],
        )
    )

    async def run():
        _current_context_var.set(ctx)
        _current_images_var.set(ctx.image_sources())
        return await read_forward.ainvoke({})

    text = asyncio.run(run())
    assert "没有找到" in text


def test_read_forward_reports_failure_instead_of_inventing():
    """卡片取不到内容时说清楚，绝不编一段出来。"""
    kernel = _FakeKernel(messages=[], accept_ids=[])
    api = _FakeAPI(kernel, event=_card_event())
    agent = _tool_agent(api=api)
    read_forward = _tool(agent, "read_forward")

    ctx = _ctx_with_history_card()

    async def run():
        _current_context_var.set(ctx)
        _current_images_var.set(ctx.image_sources())
        return await read_forward.ainvoke({})

    text = asyncio.run(run())
    assert "解析转发失败" in text
    assert "别编内容" in text


def test_read_forward_accepts_explicit_message_id():
    """模型直接指定卡片消息 ID 时照样能解析。"""
    kernel = _FakeKernel(messages=[_leaf("指定解析")])
    api = _FakeAPI(kernel, event=_card_event())
    agent = _tool_agent(api=api)
    read_forward = _tool(agent, "read_forward")

    async def run():
        _current_context_var.set(None)
        return await read_forward.ainvoke({"message_id": CARD_ID})

    text = asyncio.run(run())
    assert "指定解析" in text
    assert "指定的消息" in text


# ══════════════════════════════════════════════════════════
# OCR 的 forward 来源
# ══════════════════════════════════════════════════════════
def test_ocr_forward_source_without_expansion_is_explicit():
    """还没展开卡片就调 ocr_img(source="forward")，要给出明确的下一步。"""
    extension = _FakeExtension()
    agent = _tool_agent(api=None, extension=extension)
    ocr_img = _tool(agent, "ocr_img")

    async def run():
        _current_images_var.set({"current": [], "quote": [], "history": {}})
        return await ocr_img.ainvoke({"source": "forward"})

    text = asyncio.run(run())
    assert "read_forward" in text
    # 绝不能拿当前消息的图冒充转发里的图
    assert extension.calls == []


def test_ocr_forward_does_not_fall_back_to_current():
    """forward 没图时不回落到 current（那会让模型拿到不相干的文字）。"""
    extension = _FakeExtension()
    agent = _tool_agent(api=None, extension=extension)
    ocr_img = _tool(agent, "ocr_img")

    async def run():
        _current_images_var.set({"current": ["https://x/now.png"], "quote": []})
        return await ocr_img.ainvoke({"source": "forward"})

    text = asyncio.run(run())
    assert "read_forward" in text
    assert extension.calls == []


# ══════════════════════════════════════════════════════════
# 上下文与提示
# ══════════════════════════════════════════════════════════
def test_forward_candidates_prefers_quote_then_current_then_history():
    ctx = _ctx_with_history_card()
    where = [c["where"] for c in ctx.forward_candidates()]
    assert where == ["history"]

    ctx.current.quote = Quote(
        message_id="999",
        sender=Sender(qq="1", name="x"),
        summary="【转发聊天记录 3 条】",
        is_forward=True,
        forward_count=3,
    )
    where = [c["where"] for c in ctx.forward_candidates()]
    assert where[0] == "quote"


def test_action_hint_names_read_forward_for_record_summary():
    """「总结一下这个聊天记录」必须点名 read_forward，而不是 get_msg_history。"""
    ctx = _ctx_with_history_card()
    event = {
        "message_type": "group",
        "group_id": "1079845768",
        "user_id": "1013098110",
        "message_id": "1462453710",
        "message": [],
    }
    hint = build_action_hint(
        "总结一下这个聊天记录", event=event, is_dom=True, context=ctx
    )
    assert "read_forward" in hint
    assert "get_msg_history" not in hint


def test_action_hint_names_forward_ocr_when_asking_for_images():
    """「调用识图读这里面的图片」→ 先 read_forward 展开，再 ocr_img(source="forward")。"""
    ctx = _ctx_with_history_card()
    event = {
        "message_type": "group",
        "group_id": "1079845768",
        "user_id": "1013098110",
        "message_id": "1462453711",
        "message": [],
    }
    hint = build_action_hint(
        "调用识图读这里面的图片", event=event, is_dom=True, context=ctx
    )
    assert "read_forward" in hint
    assert 'source="forward"' in hint


def test_action_hint_keeps_history_lookup_for_plain_history_question():
    """没有卡片时，「之前说了什么」仍然走 get_msg_history。"""
    ctx = ChatContext(
        chat_type="group",
        group_id="1079845768",
        self_id="3122793026",
        current=ChatMessage(
            message_id="1",
            sender=Sender(qq="1013098110", name="Pyrin"),
            segments=[Segment("text", text="之前说了什么")],
        ),
    )
    event = {
        "message_type": "group",
        "group_id": "1079845768",
        "user_id": "1013098110",
        "message_id": "1",
        "message": [],
    }
    hint = build_action_hint(
        "之前说了什么", event=event, is_dom=True, context=ctx
    )
    assert "get_msg_history" in hint
    assert "read_forward" not in hint
