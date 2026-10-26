"""抽象层测试：用「复刻两个后端怪癖」的假 Transport 锁住归一化行为。

假后端不是随手编的，每条行为都对应 2026-10-26 在真机上观测到的现象：

- NapCat：``message_seq == message_id == 哈希``，全局序号在 ``real_seq``（str）；
  拿 ``message_id`` 当游标会返回**居中窗口**并忽略 ``reverse_order``；
  ``get_forward_msg`` 内联嵌套层，用内层 id 再查会失败。
- SnowLuma：``message_seq`` 是全局序号、``message_id`` 是另一个哈希；
  拿 ``message_seq`` 当游标会返回居中窗口；
  ``get_forward_msg`` 只给内层 id，必须再查一次。
"""

from __future__ import annotations

import asyncio

from AbstractionLayer import (
    BackendCapabilities,
    ForwardExpander,
    HistoryPager,
    MessageCursor,
    OneBotKernel,
    extract_forward_id,
)
from AbstractionLayer.kernal import NAPCAT, SNOWLUMA, CallableTransport

GROUP = 20010
TOTAL = 30
BASE_SEQ = 100


def _hash(mode: str, seq: int) -> int:
    """造一个后端本地 id：两边不同、有正有负，跟真机一样不可互比。"""
    if mode == "napcat":
        return 5_000_000 + seq * 7919
    return -(seq * 104_729) + 1_000_003


def _make_messages(mode: str) -> list[dict]:
    messages = []
    for offset in range(TOTAL):
        seq = BASE_SEQ + offset
        message = {
            "message_id": _hash(mode, seq),
            "real_id": _hash(mode, seq),
            # NapCat 的 message_seq 就是哈希；SnowLuma 的是全局序号。
            "message_seq": _hash(mode, seq) if mode == "napcat" else seq,
            "time": 1_792_000_000 + offset,
            "user_id": 10001,
            "sender": {"user_id": 10001, "nickname": "Pyrin Domain"},
            "raw_message": str(offset),
            "message": [{"type": "text", "data": {"text": str(offset)}}],
        }
        if mode == "napcat":
            message["real_seq"] = str(seq)
        messages.append(message)
    return messages


class FakeBackend:
    """复刻真机怪癖的假 OneBot。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.messages = _make_messages(mode)
        self.calls: list[tuple[str, dict]] = []

    # ── 工具 ──
    def _index_by(self, field: str, value) -> int | None:
        for index, message in enumerate(self.messages):
            if str(message.get(field)) == str(value):
                return index
        return None

    def _ending(self, index: int, count: int) -> list[dict]:
        return self.messages[max(0, index - count + 1) : index + 1]

    def _starting(self, index: int, count: int) -> list[dict]:
        return self.messages[index : index + count]

    def _centered(self, index: int, count: int) -> list[dict]:
        """真机的「忽略游标、给个居中窗口」行为。"""
        half = count // 2
        start = max(0, index - half)
        return self.messages[start : start + count]

    def _latest(self, count: int) -> list[dict]:
        return self.messages[-count:]

    # ── 假 API ──
    async def call(self, action: str, params: dict) -> dict:
        self.calls.append((action, dict(params)))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": 1, "nickname": "fake"}}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": [{"group_id": GROUP}]}
        if action == "get_group_msg_history":
            return self._history(params)
        if action == "get_forward_msg":
            return self._forward(str(params.get("message_id", "")))
        if action == "get_msg":
            return {"status": "ok", "retcode": 0, "data": {}}
        return {"status": "failed", "retcode": 400, "message": f"未知 action {action}"}

    def _history(self, params: dict) -> dict:
        count = int(params.get("count", 20))
        reverse = bool(params.get("reverse_order", False))

        if self.mode == "napcat":
            # 只认 message_seq；传 message_id 会给居中窗口（不报错）。
            if "message_seq" in params:
                index = self._index_by("message_seq", params["message_seq"])
                if index is None:
                    return {"status": "failed", "retcode": 100, "message": "消息不存在"}
                return {"status": "ok", "retcode": 0, "data": {"messages": self._ending(index, count)}}
            if "message_id" in params:
                index = self._index_by("message_id", params["message_id"])
                if index is None:
                    return {"status": "ok", "retcode": 0, "data": {"messages": self._latest(count)}}
                return {"status": "ok", "retcode": 0, "data": {"messages": self._centered(index, count)}}
            return {"status": "ok", "retcode": 0, "data": {"messages": self._latest(count)}}

        # SnowLuma：只认 message_id；传 message_seq 会被忽略。
        if "message_id" in params:
            index = self._index_by("message_id", params["message_id"])
            if index is None:
                return {"status": "ok", "retcode": 0, "data": {"messages": self._latest(count)}}
            slice_ = self._ending(index, count) if reverse else self._starting(index, count)
            return {"status": "ok", "retcode": 0, "data": {"messages": slice_}}
        return {"status": "ok", "retcode": 0, "data": {"messages": self._latest(count)}}

    def _forward(self, forward_id: str) -> dict:
        if self.mode == "napcat" and forward_id.startswith("inner"):
            return {
                "status": "failed",
                "retcode": 200,
                "message": "消息已过期或者为内层消息，无法获取转发消息",
            }

        leaves = [
            {
                "message_id": 9000 + i,
                "message_seq": 9000 + i,
                "real_id": 9000 + i,
                "time": 1_792_000_000 + i,
                "sender": {"user_id": 7, "nickname": "神也佑我阿瑶"},
                "raw_message": f"[CQ:image,file={i}.png]",
                "message": [{"type": "image", "data": {"file": f"{i}.png"}}],
            }
            for i in range(3)
        ]
        if forward_id.startswith("inner"):
            return {"status": "ok", "retcode": 0, "data": {"messages": leaves}}

        nested_seg: dict = {"type": "forward", "data": {"id": "inner-abc"}}
        if self.mode == "napcat":
            nested_seg["data"]["content"] = leaves  # 内联
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "messages": [
                    {
                        "message_id": 8001,
                        "message_seq": 8001,
                        "time": 1_792_000_000,
                        "sender": {"user_id": 9, "nickname": "转发者"},
                        "message": [nested_seg],
                    },
                    {
                        "message_id": 8002,
                        "message_seq": 8002,
                        "time": 1_792_000_001,
                        "sender": {"user_id": 9, "nickname": "转发者"},
                        "raw_message": "正文",
                        "message": [{"type": "text", "data": {"text": "正文"}}],
                    },
                ]
            },
        }


def _kernel(mode: str, caps: BackendCapabilities) -> tuple[OneBotKernel, FakeBackend]:
    fake = FakeBackend(mode)
    kernel = OneBotKernel(CallableTransport(fake.call), capabilities=caps, label=mode)
    return kernel, fake


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════
# 游标
# ══════════════════════════════════════════════════════════
def test_cursor_normalizes_global_seq_on_both_backends():
    napcat = _make_messages("napcat")[0]
    snowluma = _make_messages("snowluma")[0]

    left = MessageCursor.from_message(napcat)
    right = MessageCursor.from_message(snowluma)

    # 全局序号一致（唯一可跨后端比较的东西）
    assert left.real_seq == right.real_seq == BASE_SEQ
    # 本地 id 不一致，且不应该被拿来跨后端比较
    assert left.message_id != right.message_id
    assert not MessageCursor(message_id=1).matches(MessageCursor(message_id=2))


def test_cursor_matches_across_backends_by_global_seq():
    napcat = MessageCursor.from_message(_make_messages("napcat")[5])
    snowluma = MessageCursor.from_message(_make_messages("snowluma")[5])
    assert napcat.matches(snowluma)
    assert snowluma.matches(napcat)


def test_cursor_parse_forms():
    assert MessageCursor.parse("seq:184").real_seq == 184
    assert MessageCursor.parse("184").real_seq == 184
    assert MessageCursor.parse("id:501493501").message_id == 501493501


def test_cursor_native_value_refuses_wrong_field():
    """全局序号游标在两个后端都取不到原生翻页字段。"""
    cursor = MessageCursor.parse("seq:184")
    assert cursor.native_value("message_seq") is None
    assert cursor.native_value("message_id") is None


# ══════════════════════════════════════════════════════════
# 能力探测
# ══════════════════════════════════════════════════════════
def test_detect_identifies_each_backend():
    for mode, expected in (("napcat", NAPCAT), ("snowluma", SNOWLUMA)):
        kernel, _ = _kernel(mode, expected)
        caps = _run(kernel.detect(group_id=GROUP))
        assert caps.id == expected.id
        assert caps.history_cursor == expected.history_cursor
        assert caps.history_global_seq_field == expected.history_global_seq_field


# ══════════════════════════════════════════════════════════
# 历史翻页：归一化后就该一模一样
# ══════════════════════════════════════════════════════════
def test_page_excludes_anchor_even_though_it_is_last():
    for mode, caps in (("napcat", NAPCAT), ("snowluma", SNOWLUMA)):
        kernel, _ = _kernel(mode, caps)
        pager = HistoryPager(kernel)
        messages = _make_messages(mode)
        anchor = MessageCursor.from_message(messages[-1])

        page = _run(pager.page(GROUP, cursor=anchor, count=5))

        assert page.ok
        assert not page.stalled
        # 锚点被剔除，剩下 4 条，并且全部比锚点旧
        assert len(page.messages) == 4
        assert all(MessageCursor.from_message(m).real_seq < anchor.real_seq for m in page.messages)
        # 升序
        seqs = [MessageCursor.from_message(m).real_seq for m in page.messages]
        assert seqs == sorted(seqs)


def test_page_refuses_to_send_wrong_cursor_field():
    """同后端拿到「只有全局序号」的游标时，必须拒发，而不是改发别的字段。"""
    kernel, fake = _kernel("napcat", NAPCAT)
    pager = HistoryPager(kernel)

    page = _run(pager.page(GROUP, cursor=MessageCursor.parse("seq:110"), count=5))

    assert not page.ok
    assert "翻页字段" in page.error
    assert fake.calls == [] or all(name == "get_group_msg_history" for name, _ in fake.calls)
    assert not [call for call in fake.calls if call[0] == "get_group_msg_history"]


def test_walk_returns_identical_global_seq_on_both_backends():
    """核心断言：同一个断点，两个后端回放出完全相同的消息序列。"""
    results = {}
    for mode, caps in (("napcat", NAPCAT), ("snowluma", SNOWLUMA)):
        kernel, _ = _kernel(mode, caps)
        pager = HistoryPager(kernel)
        walk = _run(
            pager.walk(GROUP, stop_at=MessageCursor.parse(f"seq:{BASE_SEQ + 5}"), count=6)
        )
        results[mode] = [MessageCursor.from_message(m).real_seq for m in walk.messages]
        assert walk.found
        assert walk.pages >= 2  # 确实翻了多页

    assert results["napcat"] == results["snowluma"]
    # 断点之后应恰好是 BASE_SEQ+6 .. BASE_SEQ+29
    assert results["napcat"] == list(range(BASE_SEQ + 6, BASE_SEQ + TOTAL))


def test_walk_reports_reached_start_when_anchor_missing():
    kernel, _ = _kernel("snowluma", SNOWLUMA)
    pager = HistoryPager(kernel)

    walk = _run(pager.walk(GROUP, stop_at=MessageCursor.parse("seq:1"), count=6, max_pages=20))

    assert not walk.found
    assert walk.reached_start
    assert len(walk.messages) == TOTAL


def test_count_is_clamped_to_backend_limit():
    for mode, caps in (("napcat", NAPCAT), ("snowluma", SNOWLUMA)):
        kernel, fake = _kernel(mode, caps)
        pager = HistoryPager(kernel)
        _run(pager.page(GROUP, count=caps.history_max_count * 10, is_reserve=False))

        sent = [params for action, params in fake.calls if action == "get_group_msg_history"][-1]
        assert sent["count"] == caps.history_max_count
        # 两个后端都不支持 is_reserve，不应该被发出去
        assert "is_reserve" not in sent


def test_is_reserve_only_sent_when_supported():
    caps = BackendCapabilities(id="x", label="X", history_accepts_is_reserve=True)
    kernel, fake = _kernel("snowluma", caps)
    pager = HistoryPager(kernel)

    _run(pager.page(GROUP, count=3, is_reserve=True))

    sent = [params for action, params in fake.calls if action == "get_group_msg_history"][-1]
    assert sent["is_reserve"] is True


# ══════════════════════════════════════════════════════════
# 转发展开
# ══════════════════════════════════════════════════════════
def test_forward_expansion_same_result_on_both_backends():
    results = {}
    for mode, caps in (("napcat", NAPCAT), ("snowluma", SNOWLUMA)):
        kernel, fake = _kernel(mode, caps)
        expander = ForwardExpander(kernel)
        expansion = _run(expander.expand("top-xyz"))

        assert not expansion.errors, expansion.errors
        assert expansion.depth_reached == 2
        results[mode] = [entry.message["message_id"] for entry in expansion.entries]

        forward_calls = [name for name, _ in fake.calls if name == "get_forward_msg"]
        if mode == "napcat":
            # 只发一次：内联已展开，绝不该拿 inner id 再发
            assert forward_calls == ["get_forward_msg"]
            assert all(entry.via == "inline" for entry in expansion.entries if entry.depth == 2)
        else:
            # 发两次：顶层 + 内层
            assert len(forward_calls) == 2
            assert all(entry.via == "fetch" for entry in expansion.entries if entry.depth == 2)

    assert results["napcat"] == results["snowluma"]
    assert len(results["napcat"]) == 4  # 3 个内联叶子 + 1 条正文


def test_forward_degrades_when_neither_inline_nor_fetchable():
    caps = BackendCapabilities(
        id="weird",
        label="Weird",
        forward_inline_nested=False,
        forward_inner_id_fetchable=False,
    )
    kernel, _ = _kernel("snowluma", caps)
    expansion = _run(ForwardExpander(kernel).expand("top-xyz"))

    # 拿不到内层，但不能崩、不能死循环，父消息仍然保住
    assert expansion.unresolved == ["inner-abc"]
    assert len(expansion.entries) == 1  # 只剩那条正文
    assert expansion.errors


def test_forward_depth_limit_stops_recursion():
    kernel, _ = _kernel("napcat", NAPCAT)
    expansion = _run(ForwardExpander(kernel, max_depth=1).expand("top-xyz"))

    assert expansion.truncated
    assert any("max_depth" in error for error in expansion.errors)


def test_extract_forward_id_shapes():
    old_style = {"message": [{"type": "forward", "data": {"id": "7700868210009142942"}}]}
    json_style = {
        "message": [
            {
                "type": "json",
                "data": {
                    "data": (
                        '{"app":"com.tencent.multimsg","meta":{"detail":{"resid":"resid-1"}}}'
                    )
                },
            }
        ]
    }
    raw_style = {"message": [], "raw_message": "[CQ:forward,id=raw-9]"}
    opaque = "vALDksI99oBD4qt5BJFckjFGi9BzkTDBZyd6pXE1Avfkg41cYatIHmvf853D23Gm"
    opaque_style = {"message": [{"type": "forward", "data": {"id": opaque}}]}

    assert extract_forward_id(old_style) == "7700868210009142942"
    assert extract_forward_id(json_style) == "resid-1"
    assert extract_forward_id(raw_style) == "raw-9"
    # 长 base64 id 必须原样保留，含 + / = 也不能动
    assert extract_forward_id(opaque_style) == opaque
    assert extract_forward_id({"message": []}) is None


# ══════════════════════════════════════════════════════════
# 接线验证：transport/api 的游标字段来自实测，不再靠配置猜
# ══════════════════════════════════════════════════════════
class _FakeConnection:
    """冒充 OneBotConnection：只暴露 call_api / backend / history_anchor。"""

    def __init__(self, mode: str, configured_anchor: str) -> None:
        self.backend = mode
        self.history_anchor = configured_anchor
        self.is_reverse = False
        self.is_connected = True
        self.fake = FakeBackend(mode)

    async def call_api(self, action: str, params: dict, timeout=None) -> dict:
        return await self.fake.call(action, params)


def test_api_verify_history_cursor_corrects_wrong_config():
    """配置把游标字段写错时，实测会纠正回来（这是断点恢复分叉的根因）。"""
    from transport.api import OneBotAPI

    cases = [("napcat", "message_id"), ("snowluma", "message_seq")]
    for mode, wrong in cases:
        api = OneBotAPI(_FakeConnection(mode, wrong))
        assert api.configured_history_anchor == wrong

        got = asyncio.run(api.verify_history_cursor(GROUP))
        expected = "message_seq" if mode == "napcat" else "message_id"
        assert got == expected
        # 之后所有历史调用都会用实测值发包。
        assert api.history_anchor == expected


def test_api_history_params_uses_probed_cursor():
    """实测后真正发出去的参数里只带正确那个字段。"""
    from transport.api import OneBotAPI

    api = OneBotAPI(_FakeConnection("snowluma", "message_seq"))
    asyncio.run(api.verify_history_cursor(GROUP))

    sent: list[dict] = []
    original = api._call_api

    async def spy(action, params=None, timeout=None):
        sent.append(dict(params or {}))
        return await original(action, params, timeout)

    api._call_api = spy
    asyncio.run(
        api.get_group_msg_history(
            group_id=GROUP, count=5, reverse_order=True,
            message_seq=110, message_id=9_000_001,
        )
    )

    assert "message_id" in sent[-1]
    assert "message_seq" not in sent[-1], "发错字段会被后端静默忽略"


# ══════════════════════════════════════════════════════════
# 搬运：整卡搬「聊天记录套聊天记录」时两边的差异
# 实测 2026-10-26：
#   NapCat  整卡搬 → json multimsg，内层仍是 [聊天记录]，可打开
#   SnowLuma 整卡搬 → 内层压成 xml(viewMultiMsg)，m_fileName 指向源会话，
#                     跨群后失效，接收方只看到 [XML消息]
# ══════════════════════════════════════════════════════════
_MULTIMSG_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<msg brief="[聊天记录]" m_fileName="MultiMsg_C125CE25" action="viewMultiMsg" '
    'tSum="2" flag="3" serviceID="35" m_fileSize="100">'
    "<item><title>某某的聊天记录</title>"
    "<title>某某: [图片]</title><title>某某: 神了</title></item>"
    '<source name="聊天记录"></source></msg>'
)


def test_opaque_multimsg_is_recognized():
    from AbstractionLayer import is_opaque_multimsg, titles_of_multimsg

    xml_seg = {"type": "xml", "data": {"data": _MULTIMSG_XML, "resid": 35}}
    assert is_opaque_multimsg(xml_seg)

    json_seg = {
        "type": "json",
        "data": {"data": '{"app":"com.tencent.multimsg","meta":{}}'},
    }
    assert is_opaque_multimsg(json_seg)

    assert not is_opaque_multimsg({"type": "text", "data": {"text": "hi"}})
    assert not is_opaque_multimsg({"type": "xml", "data": {"data": "<msg brief=\"[链接]\"/>"}})

    assert titles_of_multimsg(xml_seg) == ["某某的聊天记录", "某某: [图片]", "某某: 神了"]


def test_plan_relay_rebuilds_opaque_nested_on_both_backends():
    """xml 死卡片与后端无关：谁整卡重发都救不回来，一律重建。"""
    from AbstractionLayer import plan_relay

    messages = [{"message": [{"type": "xml", "data": {"data": _MULTIMSG_XML}}]}]
    snow = plan_relay(SNOWLUMA, messages)
    nap = plan_relay(NAPCAT, messages)

    assert snow.action == "rebuild" and snow.opaque and snow.lossy
    assert nap.action == "rebuild" and nap.opaque


def test_plan_relay_trusts_fixed_single_forward_for_forward_segments():
    """2.2.0 起整卡搬会按目标会话重建 resid，forward 段的嵌套记录不必自己重建。"""
    from AbstractionLayer import plan_relay

    messages = [{"message": [{"type": "forward", "data": {"id": "inner-1"}}]}]
    snow = plan_relay(SNOWLUMA, messages)
    assert snow.action == "single", snow.reason
    assert snow.nested and not snow.opaque
    # 老版本（1.14.x）行为：能力标记为 False 时仍要重建。
    from dataclasses import replace

    old = plan_relay(replace(SNOWLUMA, relay_single_preserves_nested=False), messages)
    assert old.action == "rebuild"


def test_relay_degrades_opaque_nested_to_text():
    """SnowLuma 拿不到内层内容时，至少把摘要变成可打开的文本记录。"""
    from AbstractionLayer import ForwardRelay

    messages = [
        {"user_id": 1, "sender": {"nickname": "QQ用户"}, "time": 100,
         "message": [{"type": "xml", "data": {"data": _MULTIMSG_XML}}]},
        {"user_id": 1, "sender": {"nickname": "QQ用户"}, "time": 101,
         "message": [{"type": "text", "data": {"text": "这也太地狱了"}}]},
    ]
    relay = ForwardRelay(OneBotKernel(CallableTransport(_fail), capabilities=SNOWLUMA))
    content = asyncio.run(relay.collect(messages=messages))

    assert len(content.nodes) == 2
    assert content.lossy and content.degraded == ["某某的聊天记录"]
    # 第一条的 content 是「摘要 node」列表，不是 xml 段
    inner = content.nodes[0]["data"]["content"]
    assert all(seg["type"] == "node" for seg in inner)
    texts = [s["data"]["text"] for node in inner for s in node["data"]["content"]]
    assert texts == ["某某: [图片]", "某某: 神了"]


def test_relay_keeps_forward_segment_on_napcat():
    """NapCat 不能把 node 套 node 当嵌套记录（会退化成 [卡片消息]），保留 forward 段。"""
    from AbstractionLayer import ForwardRelay

    backend = FakeBackend("napcat")
    messages = [
        {"user_id": 1, "sender": {"nickname": "QQ用户"}, "time": 100,
         "message": [{"type": "forward", "data": {"id": "inner-1"}}]},
    ]
    relay = ForwardRelay(OneBotKernel(CallableTransport(backend.call), capabilities=NAPCAT))
    content = asyncio.run(relay.collect(messages=messages))

    assert len(content.nodes) == 1
    segs = content.nodes[0]["data"]["content"]
    assert segs == [{"type": "forward", "data": {"id": "inner-1"}}]


def test_relay_inlines_forward_as_nested_nodes_on_snowluma():
    """SnowLuma 认嵌套 node：内层展开成多个 node，内容自包含。"""
    from AbstractionLayer import ForwardRelay

    backend = FakeBackend("snowluma")
    inner = [
        {"user_id": 11, "sender": {"nickname": "甲"}, "time": 200,
         "message": [{"type": "text", "data": {"text": "内层A"}}]},
        {"user_id": 12, "sender": {"nickname": "乙"}, "time": 201,
         "message": [{"type": "text", "data": {"text": "内层B"}}]},
    ]
    relay = ForwardRelay(OneBotKernel(CallableTransport(lambda a, p: _reply(inner)), capabilities=SNOWLUMA))
    content = asyncio.run(relay.collect(messages=[
        {"user_id": 1, "sender": {"nickname": "外层"}, "time": 100,
         "message": [{"type": "forward", "data": {"id": "inner-1"}}]},
    ]))

    segs = content.nodes[0]["data"]["content"]
    assert [s["type"] for s in segs] == ["node", "node"]
    assert [s["data"]["name"] for s in segs] == ["甲", "乙"]
    assert segs[0]["data"]["content"][0]["data"]["text"] == "内层A"


async def _reply(messages):
    return {"status": "ok", "retcode": 0, "data": {"messages": messages}}


async def _fail(action, params):
    return {"status": "failed", "retcode": 400, "message": "should not call"}
