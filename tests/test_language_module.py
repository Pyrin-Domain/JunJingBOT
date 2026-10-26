"""LanguageModule 的结构化输出：<status> / <think> / <output><message> 解析与 chat() 返回列表。"""

import asyncio
from types import SimpleNamespace

import pytest

import agent.runtime as LanguageModule
from agent.runtime import (
    QQBotAgent,
    build_action_hint,
    looks_like_repeat,
    parse_agent_output,
    pick_scene_line,
    split_sendable_messages,
)
from agent.output import TEMPLATE_LINES, _SCENE_LINES


@pytest.fixture(autouse=True)
def _no_real_log(monkeypatch):
    """测试不写真实的 Agent/logs/*.json。"""
    monkeypatch.setattr(LanguageModule, "append_log", lambda entry: None)


def _messages(text: str) -> list[str]:
    return split_sendable_messages(parse_agent_output(text))


# ── 解析 ────────────────────────────────────────────────────
def test_parses_status_think_and_messages():
    parsed = parse_agent_output(
        """
<status>
心情: "开心"
状态: "水群"
记忆: "主人来了"
动作: "耳朵竖起来"
</status>
<think>是主人！</think>
<output>
<message>主人找我干嘛呀？</message>
<message>在忙什么呀</message>
</output>
"""
    )
    assert '心情: "开心"' in parsed["status"]
    assert parsed["think"] == "是主人！"
    assert split_sendable_messages(parsed) == ["主人找我干嘛呀？", "在忙什么呀"]


def test_renders_at_quote_and_sticker_to_cq_codes():
    assert _messages(
        '<output><message quote="12345">嗯？</message>'
        "<message><at>10001</at> 在忙吗</message>"
        "<message><sticker>http://x/y.jpg</sticker></message></output>"
    ) == [
        "[CQ:reply,id=12345] 嗯？",
        "[CQ:at,qq=10001] 在忙吗",
        "[CQ:image,file=http://x/y.jpg]",
    ]


def test_empty_message_means_silence():
    assert _messages("<output><message></message></output>") == []
    assert _messages("") == []


def test_falls_back_when_output_tag_missing():
    # 漏写 <output> 时，status/think 之外的正文仍然要被发出去，标签残留不能带到群里
    assert _messages("<think>想一下</think>直接发这句") == ["直接发这句"]
    assert _messages("<output>纯文本没有标签</output>") == ["纯文本没有标签"]
    assert _messages("<status>心情: 好</status><message>漏了output</message>") == ["漏了output"]


def test_stray_tags_are_stripped():
    assert _messages("<output><message>前半 <status>乱写的</status> 后半</message></output>") == [
        "前半 乱写的 后半"
    ]


# ── chat() 返回值 ───────────────────────────────────────────
class _StubAgent:
    """冒充 LangGraph agent，只返回一个预设的最终结果。"""

    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _payload):
        return {"messages": [{"role": "assistant", "content": self._content}]}


def _bare_agent(result_content):
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.rw_tool = None
    agent._recent_replies = {}
    agent.agent = _StubAgent(result_content)
    return agent


def test_chat_returns_message_list():
    agent = _bare_agent(
        "<status>心情: 好</status><think>嗯</think>"
        "<output><message>第一条</message><message>第二条</message></output>"
    )
    assert asyncio.run(agent.chat("在吗")) == ["第一条", "第二条"]


def test_chat_with_structured_context(monkeypatch):
    """传 ChatContext 时，模型拿到的是渲染好的场景/历史/当前，且动作检查照常注入。"""
    from messaging.models import ChatContext, ChatMessage, Segment, Sender

    agent = _bare_agent("")
    captured = {}

    async def _invoke(payload):
        captured["content"] = payload["messages"][0].content
        return {"messages": [{"role": "assistant", "content": "<output><message>嗯</message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    ctx = ChatContext(
        chat_type="group",
        group_id="20001",
        self_id="10002",
        master_ids=["10001"],
        current=ChatMessage(
            message_id="1059815617",
            sender=Sender(qq="10001", name="Pyrin", is_master=True),
            segments=[Segment("text", text="看看女装")],
        ),
    )
    assert asyncio.run(
        agent.chat(
            context=ctx,
            thread_id="g1",
            event={"message_type": "group", "group_id": 20001, "user_id": 10001},
        )
    ) == ["嗯"]

    content = captured["content"]
    assert "<scene>" in content and "<current>" in content
    assert "Pyrin(10001,主人)：看看女装" in content
    assert "群号=20001" in content
    assert content.count("看看女装") == 1  # 当前消息只在 <current> 里出现一次
    assert "[本轮动作检查]" in content and "send_dress" in content


def test_chat_returns_empty_list_when_silent():
    agent = _bare_agent("<output><message></message></output>")
    assert asyncio.run(agent.chat("在吗")) == []


def test_chat_ignores_output_after_send_tool(monkeypatch):
    """已经用过 send_message 工具时，不能再把 <output> 的文字重复发一遍。"""
    agent = _bare_agent("<output><message>重复内容</message></output>")

    async def _fake_invoke(_payload):
        # 模拟模型先调用 send_message 工具（记录在本轮 contextvars 里）
        agent._record_tool_call("send_message", {"message": "内容"}, result="成功")
        return {"messages": [{"role": "assistant", "content": "<output><message>重复内容</message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_fake_invoke)})()
    assert asyncio.run(agent.chat("在吗")) == []


# ── 沉默 vs 必须回复 ────────────────────────────────────────
def _directed_ctx(*, chat_type="group", at_me=True, quoted_me=False):
    from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

    segments = [Segment("text", text="说嘛")]
    if at_me:
        segments.insert(0, Segment("at", qq="10002"))
    current = ChatMessage(
        message_id="1059815617",
        sender=Sender(qq="10001", name="Pyrin", is_master=True),
        segments=segments,
        quote=Quote(message_id="1059815600", sender=Sender(qq="10002", name="君景"))
        if quoted_me
        else None,
    )
    return ChatContext(
        chat_type=chat_type,
        group_id="20001" if chat_type == "group" else "",
        self_id="10002",
        master_ids=["10001"],
        current=current,
    )


def test_silent_tool_does_not_swallow_reply():
    """模型同时调了 silent_observe 又写了 <message>：以回复为准，别把话吞掉。"""
    agent = _bare_agent("")
    content = "<output><message>喜欢主人呀</message></output>"

    async def _invoke(_payload):
        agent._record_tool_call("silent_observe", {"reason": "不想说"}, result="已静默")
        return {"messages": [{"role": "assistant", "content": content}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    assert asyncio.run(agent.chat("说嘛")) == ["喜欢主人呀"]


def test_directed_message_forces_reply():
    """被 @ 却选择沉默时，追加 <force_reply> 再要一次。"""
    agent = _bare_agent("")
    seen = {"calls": 0, "second": ""}

    async def _invoke(payload):
        seen["calls"] += 1
        if seen["calls"] == 1:
            agent._record_tool_call("silent_observe", {"reason": "懒得回"}, result="已静默")
            return {"messages": [{"role": "assistant", "content": "<output><message></message></output>"}]}
        seen["second"] = payload["messages"][0].content
        return {"messages": [{"role": "assistant", "content": "<output><message>在的</message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    assert asyncio.run(agent.chat(context=_directed_ctx(), thread_id="g1")) == ["在的"]
    assert seen["calls"] == 2
    assert "<force_reply>" in seen["second"]
    assert "<current>" in seen["second"]


def test_unaddressed_chatter_can_stay_silent():
    """没人 @、没人回你的群聊，沉默依然允许，不该触发重试。"""
    agent = _bare_agent("")
    seen = {"calls": 0}

    async def _invoke(_payload):
        seen["calls"] += 1
        agent._record_tool_call("silent_observe", {"reason": "跟我无关"}, result="已静默")
        return {"messages": [{"role": "assistant", "content": "<output><message></message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    assert asyncio.run(agent.chat(context=_directed_ctx(at_me=False), thread_id="g2")) is None
    assert seen["calls"] == 1


def test_private_chat_always_directed():
    assert _directed_ctx(chat_type="private", at_me=False).directed_at_me is True
    assert _directed_ctx(quoted_me=True, at_me=False).directed_at_me is True
    assert _directed_ctx(at_me=False).directed_at_me is False


# ── 续说（<next>继续/结束</next>）─────────────────────────────
def test_parse_next_variants():
    assert parse_agent_output(
        "<output><message>嗯</message><next>继续</next></output>"
    )["next"] == "continue"
    assert parse_agent_output(
        "<output><message>嗯</message><next>结束</next></output>"
    )["next"] == "end"
    assert parse_agent_output("<output><message>嗯</message></output>")["next"] == "end"


def test_next_continue_triggers_second_round():
    """<next>继续</next> 会把上一轮接回对话里，再要一轮。"""
    from langchain_core.messages import AIMessage

    agent = _bare_agent("")
    seen = []

    async def _invoke(payload):
        seen.append(list(payload["messages"]))
        if len(seen) == 1:
            return {"messages": [{"role": "assistant", "content":
                "<output><message>那我说了</message><next>继续</next></output>"}]}
        return {"messages": [{"role": "assistant", "content":
            "<output><message>其实我挺喜欢主人的</message><next>结束</next></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    out = asyncio.run(agent.chat("说嘛"))
    assert out == ["那我说了", "其实我挺喜欢主人的"]
    assert len(seen) == 2
    second = seen[1]
    assert len(second) == 3                      # 原始提问 + 自己的上一条 + 续说指令
    assert isinstance(second[1], AIMessage)
    assert "<continue>" in second[2].content


def test_dodge_reply_gets_a_second_chance():
    """「在说话啦，别急嘛」这种敷衍句，即使写了结束也要补一轮正面回答。"""
    agent = _bare_agent("")
    seen = []

    async def _invoke(payload):
        seen.append(list(payload["messages"]))
        if len(seen) == 1:
            return {"messages": [{"role": "assistant", "content":
                "<output><message>在……在说话啦，主人别急嘛。</message><next>结束</next></output>"}]}
        return {"messages": [{"role": "assistant", "content":
            "<output><message>其实一直在等主人问我这个。</message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    out = asyncio.run(agent.chat("说嘛"))
    assert out == ["在……在说话啦，主人别急嘛。", "其实一直在等主人问我这个。"]
    assert len(seen) == 2
    assert "敷衍" in seen[1][-1].content


def test_continue_rounds_are_capped():
    """一直写「继续」也不会无限刷屏：最多补 2 轮，总消息数有上限。"""
    agent = _bare_agent("")
    calls = {"n": 0}

    async def _invoke(_payload):
        calls["n"] += 1
        return {"messages": [{"role": "assistant", "content":
            f"<output><message>第{calls['n']}句</message><next>继续</next></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    out = asyncio.run(agent.chat("说嘛"))
    assert calls["n"] == 1 + LanguageModule._MAX_CONTINUE_ROUNDS
    assert len(out) == 3
    assert len(out) <= LanguageModule._MAX_OUT_MESSAGES


# ── 逐轮「动作检查」 ────────────────────────────────────────
_GROUP_EVENT = {
    "message_type": "group",
    "group_id": 20001,
    "user_id": 10001,
    "message_id": 331764385,
    "message": [
        {"type": "at", "data": {"qq": "10002"}},
        {"type": "text", "data": {"text": "看看女装"}},
    ],
}


def test_hint_names_send_dress_for_master():
    hint = build_action_hint(
        "{isDom:true}...看看女装", event=_GROUP_EVENT, is_dom=True
    )
    assert "send_dress(group_id=20001)" in hint
    assert "先进行原生工具调用" in hint


def test_hint_refuses_send_dress_for_others():
    hint = build_action_hint("看看女装", event=_GROUP_EVENT, is_dom=False)
    assert "不要**调用 send_dress" in hint


def test_hint_ignores_dress_questions():
    """提到「女装」只是在提问/讨论时，不该被判定成要照片。"""
    for text in (
        "你为什么羡慕女装？",
        "女装好看吗",
        "你穿过女装没",
        "说实话，你是不是羡慕女装",
        "我不想看女装",
        "为什么给我发女装照",
    ):
        assert build_action_hint(text, event=_GROUP_EVENT, is_dom=True) == "", text


def test_hint_fires_on_explicit_dress_request():
    for text in (
        "看看女装",
        "发张女装照",
        "来一张腿照",
        "女装照发我",
        "能发张女装照吗",
        "给我看看白丝",
    ):
        hint = build_action_hint(text, event=_GROUP_EVENT, is_dom=True)
        assert "send_dress(group_id=20001)" in hint, text


def test_send_dress_tool_refuses_when_just_talking():
    """模型自己误调 send_dress 时，工具层也要拦住「只是在聊女装」。"""
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.napcat_api = None
    agent.extension = None
    agent.mp = None
    dress = {t.name: t for t in agent._build_tools()}["send_dress"]

    LanguageModule._current_user_text_var.set("你为什么羡慕女装？")
    out = asyncio.run(dress.ainvoke({"group_id": 583261953}))
    assert "并没有要照片" in out and "文字回答" in out


def test_hint_for_poke_uses_at_target():
    event = dict(_GROUP_EVENT)
    event["message"] = [
        {"type": "at", "data": {"qq": "999888"}},
        {"type": "text", "data": {"text": "戳一戳他"}},
    ]
    hint = build_action_hint("戳一戳他", event=event, is_dom=True)
    assert "poke(user_id=999888, group_id=20001)" in hint


def test_hint_for_recall_prefers_replied_message():
    event = dict(_GROUP_EVENT)
    event["message"] = [{"type": "reply", "data": {"id": 282391592}}]
    hint = build_action_hint("把上一条撤了", event=event, is_dom=True)
    assert "recall_msg(message_id=282391592)" in hint


def test_hint_forbids_real_ban_on_joke():
    hint = build_action_hint("管理员快禁言我", event=_GROUP_EVENT, is_dom=True)
    assert "严禁" in hint and "set_ban" in hint


def test_hint_for_ocr_only_when_asked_about_image():
    event = dict(_GROUP_EVENT)
    event["message"] = [
        {"type": "image", "data": {"url": "https://x/a.png"}},
        {"type": "text", "data": {"text": "这图里写了什么"}},
    ]
    hint = build_action_hint("这图里写了什么", event=event, is_dom=True)
    assert "ocr_img()" in hint
    # URL 不再交给模型复述（长 URL 容易被抄错），由服务端按 index 解析
    assert "https://x/a.png" not in hint
    assert build_action_hint("哈哈哈", event=event, is_dom=True) == ""


def test_hint_for_ocr_mentions_index_when_multiple_images():
    event = dict(_GROUP_EVENT)
    event["message"] = [
        {"type": "image", "data": {"url": "https://x/a.png"}},
        {"type": "image", "data": {"url": "https://x/b.png"}},
        {"type": "text", "data": {"text": "这图里写了什么"}},
    ]
    hint = build_action_hint("这图里写了什么", event=event, is_dom=True)
    assert "ocr_img(index=0)" in hint and "index=0..1" in hint


def test_hint_for_ocr_targets_quoted_image():
    """当前消息只有文字、图在被引用的那条里 → 提示改用 source="quote"。"""
    from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

    quoted = Quote(
        message_id="98",
        sender=Sender(qq="10001", name="Pyrin"),
        summary="[图片]",
        images=["https://x/quoted.png"],
    )
    ctx = ChatContext(
        current=ChatMessage(
            message_id="99",
            sender=Sender(qq="10001", name="Pyrin"),
            segments=[Segment(type="text", text="这图里写了什么")],
            quote=quoted,
        )
    )
    hint = build_action_hint("这图里写了什么", event=_GROUP_EVENT, is_dom=True, context=ctx)
    assert 'ocr_img(source="quote")' in hint
    assert "https://x/quoted.png" not in hint


def test_hint_for_ocr_lists_both_sources_when_both_have_images():
    from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

    ctx = ChatContext(
        current=ChatMessage(
            message_id="99",
            sender=Sender(qq="10001", name="Pyrin"),
            segments=[Segment(type="image", url="https://x/now.png")],
            quote=Quote(
                message_id="98",
                sender=Sender(qq="10001", name="Pyrin"),
                summary="[图片]",
                images=["https://x/quoted.png"],
            ),
        )
    )
    hint = build_action_hint("这图里写了什么", event=_GROUP_EVENT, is_dom=True, context=ctx)
    assert "ocr_img()" in hint and 'ocr_img(source="quote")' in hint


def test_hint_for_ocr_history_only_on_back_reference():
    """「上一条图」才去历史里找；普通提到图片不要乱 OCR 旧图。"""
    from messaging.models import ChatContext, ChatMessage, Segment

    ctx = ChatContext(
        current=ChatMessage(
            message_id="99",
            segments=[Segment(type="text", text="上一条图里写了什么")],
        ),
        history=[
            ChatMessage(
                message_id="10",
                segments=[Segment(type="image", url="https://x/old.png")],
            )
        ],
    )
    hint = build_action_hint("上一条图里写了什么", event=_GROUP_EVENT, is_dom=True, context=ctx)
    assert 'ocr_img(source="history")' in hint
    assert "https://x/old.png" not in hint

    # 没有回指 → 不注入
    assert build_action_hint("图片挺好看的", event=_GROUP_EVENT, is_dom=True, context=ctx) == ""


class _FakeExtension:
    def __init__(self):
        self.calls: list[str] = []

    async def napcat_ocr(self, img_url: str, re_ocr: bool = False) -> dict:
        self.calls.append(img_url)
        return {"text": f"OCR:{img_url}", "raw": None}


def _tool_agent(extension):
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.napcat_api = None
    agent.extension = extension
    agent.mp = None
    agent.rw_tool = None
    agent.tools = agent._build_tools()
    return agent


def test_ocr_tool_resolves_url_from_current_message():
    """模型只传 index，URL 完全由服务端决定（模型抄错 URL 的问题从根上消失）。"""
    from messaging.models import ChatContext, ChatMessage, Segment

    extension = _FakeExtension()
    agent = _tool_agent(extension)
    ocr_tool = next(t for t in agent.tools if t.name == "ocr_img")

    ctx = ChatContext(
        current=ChatMessage(
            segments=[
                Segment(type="image", url="https://x/a.png"),
                Segment(type="image", url="https://x/b.png"),
            ]
        )
    )

    async def run():
        LanguageModule._current_images_var.set(ctx.image_sources())
        first = await ocr_tool.ainvoke({"index": 0})
        second = await ocr_tool.ainvoke({"index": 1})
        # 越界 / 非法 index 回退到第一张，而不是直接失败
        fallback = await ocr_tool.ainvoke({"index": 99})
        return first, second, fallback

    first, second, fallback = asyncio.run(run())
    assert first == "OCR:https://x/a.png"
    assert second == "OCR:https://x/b.png"
    assert fallback == "OCR:https://x/a.png"
    # 第三次是同一 URL，命中 OCR 缓存，不会再下载/识别一遍
    assert extension.calls == ["https://x/a.png", "https://x/b.png"]


def test_ocr_tool_reads_quoted_image():
    """引用/回复一张图时，source="quote" 也能拿到被引用消息的图片 URL。"""
    from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

    extension = _FakeExtension()
    agent = _tool_agent(extension)
    ocr_tool = next(t for t in agent.tools if t.name == "ocr_img")

    ctx = ChatContext(
        current=ChatMessage(
            segments=[Segment(type="text", text="这图里写了什么")],
            quote=Quote(
                message_id="98",
                sender=Sender(qq="10001", name="Pyrin"),
                summary="[图片]",
                images=["https://x/quoted.png"],
            ),
        )
    )

    async def run():
        LanguageModule._current_images_var.set(ctx.image_sources())
        return await ocr_tool.ainvoke({"source": "quote"})

    assert asyncio.run(run()) == "OCR:https://x/quoted.png"
    assert extension.calls == ["https://x/quoted.png"]


def test_ocr_tool_falls_back_between_sources():
    """模型 source 写错但另一侧有图时回退，不直接报错。"""
    agent = _tool_agent(_FakeExtension())
    ocr_tool = next(t for t in agent.tools if t.name == "ocr_img")

    async def run():
        LanguageModule._current_images_var.set(
            {"current": ["https://x/now.png"], "quote": []}
        )
        return await ocr_tool.ainvoke({"source": "quote"})

    assert asyncio.run(run()) == "OCR:https://x/now.png"


def test_ocr_tool_reads_history_image():
    """历史里的图按 message_id 取；不传 id 时取最近一张；id 没图则明确报错。"""
    from messaging.models import ChatContext, ChatMessage, Segment

    extension = _FakeExtension()
    agent = _tool_agent(extension)
    ocr_tool = next(t for t in agent.tools if t.name == "ocr_img")

    ctx = ChatContext(
        history=[
            ChatMessage(message_id="10", segments=[Segment(type="image", url="https://x/old.png")]),
            ChatMessage(message_id="11", segments=[Segment(type="text", text="没图")]),
            ChatMessage(message_id="12", segments=[Segment(type="image", url="https://x/new.png")]),
        ]
    )

    async def run():
        LanguageModule._current_images_var.set(ctx.image_sources())
        latest = await ocr_tool.ainvoke({"source": "history"})
        exact = await ocr_tool.ainvoke({"source": "history", "message_id": "10"})
        wrong = await ocr_tool.ainvoke({"source": "history", "message_id": "404"})
        return latest, exact, wrong

    latest, exact, wrong = asyncio.run(run())
    assert latest == "OCR:https://x/new.png"
    assert exact == "OCR:https://x/old.png"
    assert "没有可识别的图片" in wrong
    # 404 那条没有图，不应该随便 OCR 别的图
    assert extension.calls == ["https://x/new.png", "https://x/old.png"]


def test_ocr_tool_without_current_image_returns_hint():
    agent = _tool_agent(_FakeExtension())
    ocr_tool = next(t for t in agent.tools if t.name == "ocr_img")

    async def run():
        LanguageModule._current_images_var.set({"current": [], "quote": []})
        return await ocr_tool.ainvoke({})

    assert "没有可识别的图片" in asyncio.run(run())


def test_hint_is_empty_for_small_talk():
    assert build_action_hint("今天心情不错呀", event=_GROUP_EVENT, is_dom=True) == ""


# ── 复读检测 / 改写 ────────────────────────────────────────
def test_template_lines_are_collected_from_prompt():
    # 示例台词自动从 SYSTEM_PROMPT 抽取，改示例不用改代码
    assert "喏看完就还我别存下来" in TEMPLATE_LINES
    # 输出格式段里的占位符不该被当成台词
    assert not any("图片链接或本地图片路径" in t for t in TEMPLATE_LINES)


def test_looks_like_repeat_detects_example_copy():
    assert looks_like_repeat("喏，看完就还我，别存下来。")
    assert looks_like_repeat("喏  看完就还我 别存下来")  # 标点/空格差异也算
    assert looks_like_repeat("文明点啦，群里还有小孩子呢")


def test_looks_like_repeat_detects_own_recent_line():
    assert looks_like_repeat("拿去拿去，看完了还我", ["拿去拿去，看完了还我"])
    assert not looks_like_repeat("拿去拿去，看完了还我", ["主人找我干嘛呀？"])


def test_looks_like_repeat_ignores_fresh_text_and_cq():
    assert not looks_like_repeat("喵，人家现在不太想理你")
    assert not looks_like_repeat("[CQ:image,file=http://x/y.jpg]")


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, _messages):
        return SimpleNamespace(content=self._content)


def test_de_repeat_rewrites_copied_line():
    agent = QQBotAgent.__new__(QQBotAgent)
    agent._recent_replies = {}
    agent.llm = _FakeLLM("拿去拿去，看完了赶紧还我")
    out = asyncio.run(
        agent._de_repeat("g1", ["喏，看完就还我，别存下来。", "喵，人家不太想理你"])
    )
    assert out == ["拿去拿去，看完了赶紧还我", "喵，人家不太想理你"]


def test_de_repeat_keeps_original_when_rewrite_is_also_copied():
    agent = QQBotAgent.__new__(QQBotAgent)
    agent._recent_replies = {}
    agent.llm = _FakeLLM("文明点啦，群里还有小孩子呢")  # 改写结果又在抄示例
    out = asyncio.run(agent._de_repeat("g1", ["喏，看完就还我，别存下来。"]))
    assert out == ["喏，看完就还我，别存下来。"]


# ── 高频场景备选台词 ──────────────────────────────────────
def test_pick_scene_line_uses_master_variant_for_master():
    line = pick_scene_line("{isDom:true}主人：看看女装", True)
    assert line in _SCENE_LINES[1][2]  # 主人版：递照片
    other = pick_scene_line("{isDom:false}群友：看看女装", False)
    assert other in _SCENE_LINES[2][2]  # 群友版：拒绝
    assert pick_scene_line("今天天气不错", True) is None


def test_pick_scene_line_avoids_recent_lines():
    line = pick_scene_line("{isDom:true}主人：看看女装", True)
    assert line
    other = pick_scene_line("{isDom:true}主人：看看女装", True, [line, line, line])
    assert other and other != line


def test_de_repeat_prefers_scene_pool_without_llm():
    # 场景池命中时不该再花一次 LLM 调用（agent 连 llm 都没有）
    agent = QQBotAgent.__new__(QQBotAgent)
    agent._recent_replies = {}
    out = asyncio.run(
        agent._de_repeat(
            "g1",
            ["喏，看完就还我，别存下来。"],
            user_message="{isDom:true}主人：看看女装",
            is_dom=True,
        )
    )
    assert out and out[0] != "喏，看完就还我，别存下来。"
    assert out[0] in _SCENE_LINES[1][2]


def test_remember_replies_keeps_recent_window():
    agent = QQBotAgent.__new__(QQBotAgent)
    agent._recent_replies = {}
    agent._remember_replies("g1", [f"第{i}条消息呀" for i in range(12)])
    assert len(agent._recent_replies_of("g1")) == 8


def test_hint_for_vague_photo_request_from_master():
    hint = build_action_hint("发一张喵", event=_GROUP_EVENT, is_dom=True)
    assert "send_dress(group_id=20001)" in hint

    # 普通群友不享受这个兜底，而且要被提醒别嘴上答应
    other = build_action_hint("发一张喵", event=_GROUP_EVENT, is_dom=False)
    assert "不要**调用 send_dress" in other


def test_chat_adds_continue_hint_when_someone_replies_to_me():
    """对方回复 Bot 上一条时，末尾要带 <continuing>，教它顺着聊。"""
    from messaging.models import ChatContext, ChatMessage, Quote, Segment, Sender

    agent = _bare_agent("")
    captured = {}

    async def _invoke(payload):
        captured["content"] = payload["messages"][0].content
        return {"messages": [{"role": "assistant", "content": "<output><message>嗯嗯</message></output>"}]}

    agent.agent = type("_A", (), {"ainvoke": staticmethod(_invoke)})()
    ctx = ChatContext(
        chat_type="group",
        group_id="20001",
        self_id="10002",
        master_ids=["10001"],
        current=ChatMessage(
            message_id="2",
            sender=Sender(qq="10001", name="Pyrin"),
            segments=[Segment("text", text="那你平时穿什么")],
            quote=Quote(
                message_id="1",
                sender=Sender(qq="10002", name="君景", is_self=True),
                summary="裙子的自由吧",
            ),
        ),
    )
    assert asyncio.run(agent.chat(context=ctx, thread_id="g1")) == ["嗯嗯"]
    content = captured["content"]
    assert "<continuing>" in content
    assert "别重复自己说过的话" in content
