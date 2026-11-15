"""审计群「复搬」：放行登记 + 标签解析 + 命令流程。

锁住这几件事：

1. 标签文本解析：``【过滤标签】`` + ``来源群：`` + ``消息：`` 三样齐才认；
2. ``ImageAuditGate.approve`` 放行是**一次性**的——复搬那一次过滤门闭嘴，
   之后同一内容再进来照样过滤（防放行消息反复触发搬运）；
3. 命令守卫：不在审计群不动作；没回复标签要提示；回复的不是标签要提示；
4. 复搬调 ``forward.forward`` 时必须 ``dedup=False``（拦截前指纹已 reserve，
   再走去重只会原地踏步）。
"""

from __future__ import annotations

import asyncio

from bot.commands.audit_release import _repost, audit_release, parse_tag
from bot.services.forward.image_gate import ImageAuditGate
from messaging.image_filter import FilterHit, ImageProbe


_TAG_TEXT = (
    "【过滤标签】色图\n"
    "来源群：1079845768\n"
    "发送者：某人（123）\n"
    "消息：1375350188\n"
    "详情：\n"
    "1. #1 色图（nsfw） score=0.9"
)


# ── 假件 ──
class _FakeHub:
    def __init__(self, hits=None):
        self.hits = list(hits or [])
        self.screen_calls = 0

    async def screen_event(self, event):
        self.screen_calls += 1
        return list(self.hits)

    async def screen_messages(self, messages, *, event=None):
        self.screen_calls += 1
        return list(self.hits)

    def note_event(self, event):
        pass

    def load_image(self, url, timeout=8.0):
        return None


def _hit() -> FilterHit:
    return FilterHit(filter_id="nsfw", label="色图", probe=ImageProbe(), score=0.9)


class _FakeMP:
    def __init__(self, reply_id=None):
        self.reply_id = reply_id
        self.sent: list = []

    def is_reply(self, _event):
        return self.reply_id

    async def send_message(self, event, message):
        self.sent.append(message)


class _FakeAPI:
    def __init__(self, messages: dict):
        self.messages = messages  # message_id -> event

    async def get_message(self, message_id):
        event = self.messages.get(str(message_id))
        if event is None:
            return {}
        return {"data": dict(event)}


class _FakeGate(ImageAuditGate):
    """不走真实 hub 的门：只测放行逻辑。"""

    def __init__(self):
        super().__init__(settings={"image_filter": {"enable": True}})
        self.hub = _FakeHub([_hit()])


class _FakeForward:
    def __init__(self, gate):
        self.image_gate = gate
        self.calls: list = []

    async def forward(self, event=None, is_recover=False, **kwargs):
        self.calls.append((event, kwargs))
        return True


class _FakeSettings:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        node = self.data
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def auto_forward_kwargs(self):
        return {"groupid_list": [1079845768], "target_groupid": [10001], "dedup": True}


class _FakeHandler:
    def __init__(self, mp, api, settings, forward):
        self.mp = mp
        self.api = api
        self.settings = settings
        self.forward = forward


# ── 标签解析 ──
def test_parse_tag_extracts_group_and_message():
    assert parse_tag(_TAG_TEXT) == ("1079845768", "1375350188")


def test_parse_tag_handles_negative_message_id():
    """QQ 消息 id 可以是负数（SnowLuma 实测），正则必须带负号。"""
    text = (
        "【过滤标签】黑白图待人工\n"
        "来源群：1105841698\n"
        "发送者：蓝色大肥鱼（319666906）\n"
        "消息：-781141664\n"
        "详情：\n"
        "1. #1 黑白图待人工（nsfw） score=0.6294"
    )
    assert parse_tag(text) == ("1105841698", "-781141664")


def test_parse_tag_rejects_non_tag_text():
    assert parse_tag("随便聊聊 来源群：123 消息：456") == ("", "")
    assert parse_tag("【过滤标签】但没有定位信息") == ("", "")


# ── 放行登记 ──
def test_gate_approve_bypasses_screen_once():
    asyncio.run(_approve_bypasses_once())


async def _approve_bypasses_once():
    gate = _FakeGate()
    event = {"group_id": 1079845768, "message_id": "m1"}
    assert gate.approve(1079845768, "m1") is True
    # 第一次：放行生效，过滤门一次都不跑
    assert await gate.screen_event(event) == []
    assert gate.hub.screen_calls == 0
    # 第二次：放行已消费，恢复过滤
    assert await gate.screen_event(event) == gate.hub.hits
    assert gate.hub.screen_calls == 1
    # chat record 路径同样只放行一次
    assert gate.approve(1079845768, "m1") is True
    assert await gate.screen_messages([{"type": "image"}], event=event) == []
    assert await gate.screen_messages([{"type": "image"}], event=event) == gate.hub.hits


def test_gate_screen_messages_without_event_still_screens():
    asyncio.run(_screen_messages_without_event())


async def _screen_messages_without_event():
    gate = _FakeGate()
    assert await gate.screen_messages([{"type": "image"}]) == gate.hub.hits
    assert gate.hub.screen_calls == 1


def test_gate_approve_expired_rescreens():
    asyncio.run(_approve_expired())


async def _approve_expired():
    import time as time_mod

    gate = _FakeGate()
    assert gate.approve("g", "m") is True
    # 登记时间拨回 TTL 之前
    key = "g:m"
    gate._approved[key] = time_mod.time() - 3600
    assert gate._approved_now({"group_id": "g", "message_id": "m"}) is False
    assert key not in gate._approved  # 过期顺手清掉


# ── 命令流程 ──
def _make_handler(gate, reply_id=None, extra_messages=None):
    tag_event = {"message_id": "tag1", "group_id": 1032125562, "message": [
        {"type": "text", "data": {"text": _TAG_TEXT}}
    ]}
    original = {"message_id": "1375350188", "group_id": 1079845768, "message": [
        {"type": "image", "data": {"url": "http://x/1.jpg"}}
    ]}
    messages = {"tag1": tag_event, "1375350188": original}
    messages.update(extra_messages or {})
    mp = _FakeMP(reply_id=reply_id)
    api = _FakeAPI(messages)
    settings = _FakeSettings({
        "image_filter": {"audit_group_id": 1032125562, "audit_operators": []},
    })
    handler = _FakeHandler(mp, api, settings, _FakeForward(gate))
    return handler, mp


def _cmd_event(group_id=1032125562, user_id="999"):
    return {"message_id": "cmd1", "group_id": group_id, "user_id": user_id}


def test_audit_release_outside_audit_group_is_noop():
    asyncio.run(_outside_noop())


async def _outside_noop():
    gate = _FakeGate()
    handler, mp = _make_handler(gate, reply_id="tag1")
    await audit_release(handler, _cmd_event(group_id=999))
    assert mp.sent == []
    assert not gate._approved
    assert handler.forward.calls == []


def test_audit_release_without_reply_hints():
    asyncio.run(_no_reply_hint())


async def _no_reply_hint():
    gate = _FakeGate()
    handler, mp = _make_handler(gate, reply_id=None)
    await audit_release(handler, _cmd_event())
    assert len(mp.sent) == 1
    assert "过滤标签" in mp.sent[0]
    assert not gate._approved


def test_audit_release_reply_to_non_tag_hints():
    asyncio.run(_non_tag_hint())


async def _non_tag_hint():
    gate = _FakeGate()
    other = {"message_id": "other", "message": [
        {"type": "text", "data": {"text": "普通聊天"}}
    ]}
    handler, mp = _make_handler(gate, reply_id="other", extra_messages={})
    handler.api.messages["other"] = other
    await audit_release(handler, _cmd_event())
    assert len(mp.sent) == 1
    assert "过滤标签" in mp.sent[0]
    assert not gate._approved


def test_audit_release_happy_path_reposts_without_dedup():
    asyncio.run(_happy_path())


async def _happy_path():
    gate = _FakeGate()
    handler, mp = _make_handler(gate, reply_id="tag1")
    await audit_release(handler, _cmd_event())
    # 放行已登记（登记的是原消息，不是命令消息）
    assert gate._approved.get("1079845768:1375350188") is not None
    assert len(handler.forward.calls) == 1
    event, kwargs = handler.forward.calls[0]
    assert event["message_id"] == "1375350188"
    assert kwargs["dedup"] is False
    assert any("重新搬运" in text for text in mp.sent)


def test_repost_twice_is_rejected():
    asyncio.run(_repost_twice())


async def _repost_twice():
    gate = _FakeGate()
    handler, mp = _make_handler(gate, reply_id="tag1")
    original = {"message_id": "1375350188", "group_id": "1079845768"}
    tag_event = {"group_id": 1032125562}
    await _repost(handler, original, tag_event)
    assert len(handler.forward.calls) == 1
    await _repost(handler, original, tag_event)
    assert len(handler.forward.calls) == 1  # 第二次被拒
    assert any("已经放行过" in text for text in mp.sent)
