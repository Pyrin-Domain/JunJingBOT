"""批量历史工具（``fetch_history`` / ``digest_user``）。

现场：群里让 Bot 翻 1000 条消息，``get_msg_history`` 一次只能回 51 条，模型
只能自己翻二十轮（日志里它自己也说「得翻二十轮」）。这里覆盖新加的两个工具：

1. 跨页攒够 N 条（不依赖模型自己翻），锚点剔除/去重/到头判定都对；
2. 从一大段里挑出某个人的发言 + 相关对话（@他 / 回复他 / 他回复谁），
   统计由代码算；
3. 原来的 ``get_msg_history`` 一个字节都没动。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.result_vault import ResultVault, thread_key
from agent.tools.history import build_history_tools
from agent.tools.history_bulk import (
    build_history_bulk_tools,
    collect_messages,
    to_messages,
)
from agent.tools.registry import _current_context_var, reset_turn_state
from messaging.models import ChatContext, ChatMessage, Sender
from transport.api import OneBotAPI

GROUP = 583261953
TARGET = "1013098110"
TARGET_NAME = "好男孩"
OTHER = "3122793026"
OTHER_NAME = "君景"
BASE_SEQ = 1000


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════
# 假后端：只实现 get_group_msg_history（NapCat 形态：seq == message_id）
# ══════════════════════════════════════════════════════════
class FakeHistory:
    backend = "napcat"
    history_anchor = "message_seq"

    def __init__(self, events):
        self.events = events
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None, timeout=None):
        params = params or {}
        self.calls.append((action, dict(params)))
        if action != "get_group_msg_history":
            return {"status": "ok", "data": {}}
        count = int(params.get("count") or 20)
        anchor = params.get(self.history_anchor)
        if anchor is not None:
            index = next(
                (
                    i
                    for i, event in enumerate(self.events)
                    if str(event["message_id"]) == str(anchor)
                ),
                None,
            )
            if index is None:
                return {"status": "ok", "data": {"messages": []}}
            end = index + 1
        else:
            end = len(self.events)
        start = max(0, end - count)
        return {
            "status": "ok",
            "data": {"messages": [dict(item) for item in self.events[start:end]]},
        }


class FakeAgent:
    def __init__(self, api):
        self.api = api
        self.messaging = None
        self.result_vault = ResultVault()


def _event(seq: int, uid: str, name: str, text: str = "", extra=None) -> dict:
    segments = list(extra or [])
    segments.append({"type": "text", "data": {"text": text or f"消息{seq}"}})
    return {
        "message_id": seq,
        "message_seq": seq,
        "real_seq": str(seq),
        "time": 1762000000 + (seq - BASE_SEQ) * 60,
        "group_id": GROUP,
        "user_id": uid,
        "sender": {"user_id": uid, "nickname": name},
        "message": segments,
        "raw_message": text,
    }


def _chat(*, others: int, target_every: int = 3) -> list[dict]:
    """前 ``others`` 条普通群聊，每 ``target_every`` 条里有一条是目标说的。"""
    events = []
    for offset in range(others):
        seq = BASE_SEQ + offset
        if offset % target_every == 0:
            events.append(_event(seq, TARGET, TARGET_NAME))
        else:
            events.append(_event(seq, OTHER, OTHER_NAME))
    return events


@pytest.fixture(autouse=True)
def _clean_turn_state():
    reset_turn_state()
    _current_context_var.set(None)
    yield
    reset_turn_state()
    _current_context_var.set(None)


# ══════════════════════════════════════════════════════════
# 翻页
# ══════════════════════════════════════════════════════════
def test_collect_messages_pages_beyond_one_request():
    """250 条必须跨页拿到，且是最新的 250 条（升序）。"""
    events = _chat(others=300)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    from agent.tools.history_bulk import _pager

    result = _run(collect_messages(_pager(agent), GROUP, 250))

    assert result.ok
    assert result.pages >= 3, "单页上限 100，250 条至少要 3 页"
    assert len(result.messages) == 250
    assert result.messages[0]["message_id"] == BASE_SEQ + 50
    assert result.messages[-1]["message_id"] == BASE_SEQ + 299


def test_collect_messages_stops_at_history_start():
    events = _chat(others=40)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    from agent.tools.history_bulk import _pager

    result = _run(collect_messages(_pager(agent), GROUP, 500))

    assert result.exhausted is True
    assert len(result.messages) == 40


def test_collect_messages_from_cursor():
    events = _chat(others=100)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    from agent.tools.history_bulk import _pager

    cursor = {"message_id": BASE_SEQ + 59, "message_seq": BASE_SEQ + 59}
    result = _run(collect_messages(_pager(agent), GROUP, 20, before=cursor))

    # 锚点是 exclusive：从 1059 往前翻拿到的是 1039~1058（不含 1059 自己）
    assert [m["message_id"] for m in result.messages] == list(
        range(BASE_SEQ + 39, BASE_SEQ + 59)
    )


def test_collect_messages_caps_at_ten_thousand_and_scales_pages():
    """上限 1 万，页数预算也得跟着放大。

    现场：「6000 翻不动，单次上限 2000」。把上限改成 1 万之后，如果页数还按
    120 页封顶，单页 40 条就只能翻到 4800 —— 上限形同虚设，所以这里一起钉住。
    """
    events = _chat(others=10050)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    from agent.tools.history_bulk import MAX_FETCH, _pager

    pager = _pager(agent)
    pager.default_count = 40  # 模拟单页 40 条的后端：1 万条要 250 页
    result = _run(collect_messages(pager, GROUP, 999999))

    assert MAX_FETCH == 10000
    assert result.ok
    assert len(result.messages) == 10000          # 超出的部分被上限夹住
    assert result.pages >= 250                    # 不是 120 页就收手
    assert result.messages[-1]["message_id"] == BASE_SEQ + 10049
    assert result.messages[0]["message_id"] == BASE_SEQ + 50


# ══════════════════════════════════════════════════════════
# 渲染 / 引用
# ══════════════════════════════════════════════════════════
def test_to_messages_fills_quote_from_batch():
    events = [
        _event(BASE_SEQ, TARGET, TARGET_NAME, "看看这个"),
        _event(
            BASE_SEQ + 1,
            OTHER,
            OTHER_NAME,
            "哪个",
            extra=[{"type": "reply", "data": {"id": str(BASE_SEQ)}}],
        ),
    ]

    messages = to_messages(events, self_id=OTHER, master_ids=(TARGET,))

    assert messages[0].sender.is_master is True
    assert messages[1].reply_to_id == str(BASE_SEQ)
    assert messages[1].quote is not None
    assert messages[1].quote.sender.qq == TARGET
    assert "看看这个" in messages[1].quote.summary


def test_fetch_history_tool_reports_span_and_count():
    events = _chat(others=150)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["fetch_history"]
    text = _run(tool.ainvoke({"count": 120, "group_id": GROUP}))

    assert text.startswith("翻到 120 条")
    assert "消息1149" in text
    assert "消息1029" not in text  # 只要最新的 120 条


def test_fetch_history_vaults_full_text_when_display_truncated(monkeypatch):
    """显示按篇幅截断，但暂存的必须是完整原文。

    翻 1 万条时 12000 字只够放最新一两百条；如果 vault 也只存这一小段，
    「已暂存」就是假的 —— 模型回头 recall_result 拿不到前面那些。
    """
    import agent.tools.history_bulk as history_bulk

    events = _chat(others=300)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)
    context = ChatContext(chat_type="group", group_id=str(GROUP))
    _current_context_var.set(context)
    monkeypatch.setattr(history_bulk, "FETCH_MAX_CHARS", 300)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["fetch_history"]
    text = _run(tool.ainvoke({"count": 200, "group_id": GROUP}))

    assert "另有更早的" in text and "因篇幅未显示" in text
    assert "result_id=R1" in text
    assert "消息1100" not in text          # 显示只留了最新几条

    entry = agent.result_vault.get(thread_key(context), "R1")
    assert entry is not None
    assert "消息1100" in entry.text        # 暂存的是完整 200 条
    assert "消息1299" in entry.text
    assert len(entry.text) > len(text)


def test_fetch_history_uses_current_group_when_omitted():
    events = _chat(others=30)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)
    _current_context_var.set(ChatContext(chat_type="group", group_id=str(GROUP)))

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["fetch_history"]
    text = _run(tool.ainvoke({"count": 10}))

    assert text.startswith("翻到 10 条")


# ══════════════════════════════════════════════════════════
# 提炼某人
# ══════════════════════════════════════════════════════════
def test_digest_user_picks_his_messages_and_mentions():
    events = _chat(others=60)
    # 别人 @ 他
    events.append(
        _event(
            BASE_SEQ + 60,
            OTHER,
            OTHER_NAME,
            "出来说话",
            extra=[{"type": "at", "data": {"qq": TARGET}}],
        )
    )
    # 别人回复他（reply 指向第 0 条，那条是他发的）
    events.append(
        _event(
            BASE_SEQ + 61,
            OTHER,
            OTHER_NAME,
            "同意",
            extra=[{"type": "reply", "data": {"id": str(BASE_SEQ)}}],
        )
    )
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["digest_user"]
    text = _run(
        tool.ainvoke({"user": TARGET, "count": 62, "context": 1, "group_id": GROUP})
    )

    assert "目标=好男孩(1013098110)" in text
    assert "发言 20/62" in text
    assert "★ " in text            # 他说的
    assert "@ " in text            # @ 他的
    assert "出来说话" in text
    assert "口头禅" in text


def test_digest_user_accepts_nickname():
    events = _chat(others=30)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["digest_user"]
    text = _run(tool.ainvoke({"user": TARGET_NAME, "count": 30, "group_id": GROUP}))

    assert "目标=好男孩(1013098110)" in text


def test_digest_user_defaults_to_current_sender():
    events = _chat(others=30)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)
    _current_context_var.set(
        ChatContext(
            chat_type="group",
            group_id=str(GROUP),
            current=ChatMessage(sender=Sender(qq=TARGET, name=TARGET_NAME)),
        )
    )

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["digest_user"]
    text = _run(tool.ainvoke({"count": 30, "group_id": GROUP}))

    assert "目标=好男孩(1013098110)" in text


def test_digest_user_reports_unknown_user():
    events = _chat(others=20)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["digest_user"]
    text = _run(
        tool.ainvoke({"user": "查无此人", "count": 20, "group_id": GROUP})
    )

    assert "没找到" in text


def test_digest_user_without_group_is_unavailable():
    api = OneBotAPI(FakeHistory(_chat(others=5)))
    agent = FakeAgent(api)

    tool = {t.name: t for t in build_history_bulk_tools(agent)}["digest_user"]
    text = _run(tool.ainvoke({"user": TARGET}))

    assert "当前不是群聊" in text


# ══════════════════════════════════════════════════════════
# 跨轮留存（现场：翻完 1500 条说「要不要挑几句给你看」，下一句「挑」结果没了）
# ══════════════════════════════════════════════════════════
def test_digest_result_survives_into_next_turn():
    events = _chat(others=40)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)
    context = ChatContext(chat_type="group", group_id=str(GROUP))
    _current_context_var.set(context)

    tools = {t.name: t for t in build_history_bulk_tools(agent)}
    first = _run(
        tools["digest_user"].ainvoke(
            {"user": TARGET, "count": 40, "context": 1, "group_id": GROUP}
        )
    )
    assert "result_id=R1" in first

    # 下一轮：工具缓存清了（vault 不清），提示里必须还看得见这条
    reset_turn_state()
    hint = agent.result_vault.render_hint(thread_key(context))
    assert "R1" in hint
    assert "recall_result" in hint

    back = _run(tools["recall_result"].ainvoke({"result_id": "R1"}))
    assert back.startswith("[R1 digest_user")
    assert "★" in back
    assert "发言 14/40" in back


def test_recall_result_defaults_to_latest_and_pages():
    events = _chat(others=40)
    api = OneBotAPI(FakeHistory(events))
    agent = FakeAgent(api)
    _current_context_var.set(ChatContext(chat_type="group", group_id=str(GROUP)))

    tools = {t.name: t for t in build_history_bulk_tools(agent)}
    _run(
        tools["digest_user"].ainvoke(
            {"user": TARGET, "count": 40, "context": 0, "group_id": GROUP}
        )
    )

    part1 = _run(tools["recall_result"].ainvoke({"max_chars": 300}))
    assert part1.startswith("[R1 ")
    assert "还有" in part1 and "offset=300" in part1

    part2 = _run(tools["recall_result"].ainvoke({"offset": 300, "max_chars": 300}))
    assert part2.startswith("[R1 ")
    assert "300~600" in part2


def test_recall_result_reports_missing_id():
    api = OneBotAPI(FakeHistory(_chat(others=5)))
    agent = FakeAgent(api)
    _current_context_var.set(ChatContext(chat_type="group", group_id=str(GROUP)))

    tools = {t.name: t for t in build_history_bulk_tools(agent)}
    text = _run(tools["recall_result"].ainvoke({"result_id": "R99"}))

    assert "没找到" in text
    assert "R99" in text


def test_vault_expires_and_keeps_recent_only():
    vault = ResultVault(ttl=-1, max_entries=2)
    vault.put("group_1", "t", "a")
    assert vault.get("group_1") is None  # ttl<0 立刻过期

    vault = ResultVault(max_entries=2)
    for index in range(3):
        vault.put("group_1", "t", f"text{index}")
    assert [e.result_id for e in vault.entries("group_1")] == ["R2", "R3"]
    assert vault.get("group_1").text == "text2"


def test_thread_key_matches_thread_id_of():
    assert thread_key(ChatContext(chat_type="group", group_id="42")) == "group_42"
    private = ChatContext(
        chat_type="private", current=ChatMessage(sender=Sender(qq="10001"))
    )
    assert thread_key(private) == "private_10001"
    assert thread_key(None) == ""


# ══════════════════════════════════════════════════════════
# 原有工具不受影响
# ══════════════════════════════════════════════════════════
def test_original_history_tools_untouched():
    api = OneBotAPI(FakeHistory(_chat(others=5)))
    agent = FakeAgent(api)

    names = [t.name for t in build_history_tools(agent)]

    assert names == ["get_msg_history", "get_message"]
