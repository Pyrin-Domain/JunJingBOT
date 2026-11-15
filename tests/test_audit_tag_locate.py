"""审计标签的定位信息必须是**外层**消息 id —— 复搬全靠它取回原消息。

事故原委（线上实测）：聊天记录命中时，标签里的「消息：」写的是**内层**那条
记录的 id（``1793527070``），复搬拿它去 ``get_msg`` 一律 "message not found"
→ 机器人只能回「取不回原消息」。内层 id 在两个后端都取不回（SnowLuma /
NapCat 实测），``get_msg`` 只认外层消息。

所以这里锁住几件事：

1. 聊天记录命中时，标签的「来源群：/ 消息：」写的是**外层卡片**的群号和 id；
2. 内层 id 不丢：详情行补一句「[内层 xxx]」，人工排查「记录里第几条」被拦用；
3. 单图命中不受影响（``event == message``，本来就是同一条消息）；
4. 端到端：标签 → 回复「复搬」→ ``get_msg(外层 id)`` 取回卡片 → 放行键 =
   ``群:外层 id`` → 按外层事件重搬。
"""

from __future__ import annotations

import asyncio

from bot.commands.audit_release import audit_release, parse_tag
from bot.services.forward.image_gate import ImageAuditGate, _format_tag_text
from messaging.image_filter import FilterHit, ImageProbe, probes_from_messages


# 外层 = 群里那条聊天记录卡片本身；内层 = 记录里的某一条消息
OUTER_GROUP = "1105841698"
OUTER_MID = "-2133034063"
INNER_MID = "1793527070"


def _outer_event() -> dict:
    """外层卡片事件（生产路径 ``screen_messages(messages, event=event)`` 传的就是它）。"""
    return {
        "message_type": "group",
        "group_id": int(OUTER_GROUP),
        "user_id": 319666906,
        "message_id": int(OUTER_MID),
        "message": [{"type": "forward", "data": {"id": "hs7oiPPNhtqMFWX5IZzU5dWcsJp27apKxdWOJf6CU0"}}],
    }


def _inner_messages() -> list[dict]:
    """记录里的两条消息：第二条带图（内层 id ≠ 外层 id）。"""
    return [
        {
            "user_id": 1094950020,
            "time": 1792736000,
            "message_id": 1,
            "sender": {"user_id": 1094950020, "nickname": "QQ用户"},
            "message": [{"type": "text", "data": {"text": "看图"}}],
        },
        {
            "user_id": 1094950020,
            "time": 1792736001,
            "message_id": int(INNER_MID),
            "sender": {"user_id": 1094950020, "nickname": "QQ用户"},
            "message": [
                {"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}}
            ],
        },
    ]


def _record_probe() -> ImageProbe:
    """生产路径下聊天记录命中时拿到的那个 probe。"""
    probes = probes_from_messages(_inner_messages(), event=_outer_event())
    assert probes, "带图的记录应该产出 probe"
    return probes[0]


def _hit(probe: ImageProbe) -> FilterHit:
    return FilterHit(filter_id="nsfw", label="色图", probe=probe, score=0.9)


# ══════════════════ probe 语义：内层 id 归内层，外层 id 归外层 ══════════════════
def test_probe_keeps_inner_message_id_but_carries_outer_event():
    probe = _record_probe()
    # probe.message_id 仍然是「图片所在那条消息」= 内层
    assert probe.message_id == INNER_MID
    # 外层卡片事件挂在 probe.event 上（复搬的定位信息从这儿取）
    assert probe.event["message_id"] == int(OUTER_MID)
    assert probe.event["group_id"] == int(OUTER_GROUP)


# ══════════════════ 标签文本：定位信息写外层 ══════════════════
def test_tag_text_uses_outer_message_id_for_chat_record():
    text = _format_tag_text([_hit(_record_probe())])

    assert f"消息：{OUTER_MID}" in text
    assert f"消息：{INNER_MID}" not in text, "内层 id 不能出现在「消息：」那一行"
    assert f"来源群：{OUTER_GROUP}" in text
    # 复搬解析出来的必须是外层
    assert parse_tag(text) == (OUTER_GROUP, OUTER_MID)


def test_tag_text_keeps_inner_id_in_details_for_troubleshooting():
    text = _format_tag_text([_hit(_record_probe())])
    assert f"[内层 {INNER_MID}]" in text
    assert "#2" in text, "详情要指明是记录里的第几条"


def test_tag_text_single_image_unchanged():
    """单图：event == message，定位信息就是那条消息本身。"""
    event = {
        "message_type": "group",
        "group_id": 740726382,
        "user_id": 3792417235,
        "message_id": 1652911697,
        "message": [{"type": "image", "data": {"url": "https://cdn/a.png"}}],
    }
    probe = ImageProbe(
        url="https://cdn/a.png",
        event=event,
        message=event,
        group_id="740726382",
        sender_id="3792417235",
        message_id="1652911697",
    )
    text = _format_tag_text([_hit(probe)])
    assert parse_tag(text) == ("740726382", "1652911697")
    assert "[内层" not in text, "单图没有内外层之分，不用补内层标记"


# ══════════════════ 端到端：标签 → 复搬 → 取回外层卡片 → 重搬 ══════════════════
class _FakeMP:
    def __init__(self, reply_id):
        self.reply_id = reply_id
        self.sent: list[str] = []

    def is_reply(self, _event):
        return self.reply_id

    async def send_message(self, _event, message):
        self.sent.append(message)


class _FakeAPI:
    def __init__(self, messages: dict):
        self.messages = messages
        self.asked: list[str] = []

    async def get_message(self, message_id):
        self.asked.append(str(message_id))
        event = self.messages.get(str(message_id))
        if event is None:
            return {}
        return {"data": dict(event)}


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
        return {"groupid_list": [int(OUTER_GROUP)], "target_groupid": [10001], "dedup": True}


class _FakeHandler:
    def __init__(self, mp, api, settings, forward):
        self.mp = mp
        self.api = api
        self.settings = settings
        self.forward = forward


def test_audit_release_reposts_outer_card_end_to_end():
    """复搬走通：拿外层 id 取回卡片 → 放行键 = 群:外层 id → 按外层事件重搬。"""
    tag_text = _format_tag_text([_hit(_record_probe())])
    tag_event = {
        "message_id": "tag1",
        "group_id": 1032125562,
        "message": [{"type": "text", "data": {"text": tag_text}}],
    }
    outer_event = _outer_event()
    api = _FakeAPI({"tag1": tag_event, OUTER_MID: outer_event})
    gate = ImageAuditGate(settings={"image_filter": {"enable": True}})
    mp = _FakeMP(reply_id="tag1")
    settings = _FakeSettings(
        {"image_filter": {"audit_group_id": 1032125562, "audit_operators": []}}
    )
    forward = _FakeForward(gate)
    handler = _FakeHandler(mp, api, settings, forward)

    asyncio.run(
        audit_release(
            handler,
            {"message_id": "cmd1", "group_id": 1032125562, "user_id": "999"},
        )
    )

    # 取回的是外层卡片，不是内层记录
    assert api.asked == ["tag1", OUTER_MID]
    # 放行键 = 来源群:外层消息 id
    assert gate._approved.get(f"{OUTER_GROUP}:{OUTER_MID}") is not None
    # 重搬的是外层卡片事件（带 forward 段）
    assert len(forward.calls) == 1
    event, kwargs = forward.calls[0]
    assert str(event["message_id"]) == OUTER_MID
    assert event["message"][0]["type"] == "forward"
    assert kwargs["dedup"] is False
    assert any("重新搬运" in text for text in mp.sent)


def test_audit_release_approve_key_matches_screen_gate():
    """放行键必须和复搬时 ``screen_messages`` 算的键一致，否则放行形同虚设。"""
    tag_text = _format_tag_text([_hit(_record_probe())])
    tag_event = {
        "message_id": "tag1",
        "group_id": 1032125562,
        "message": [{"type": "text", "data": {"text": tag_text}}],
    }
    outer_event = _outer_event()
    api = _FakeAPI({"tag1": tag_event, OUTER_MID: outer_event})
    gate = ImageAuditGate(settings={"image_filter": {"enable": True}})
    mp = _FakeMP(reply_id="tag1")
    settings = _FakeSettings(
        {"image_filter": {"audit_group_id": 1032125562, "audit_operators": []}}
    )
    forward = _FakeForward(gate)
    handler = _FakeHandler(mp, api, settings, forward)

    asyncio.run(
        audit_release(
            handler,
            {"message_id": "cmd1", "group_id": 1032125562, "user_id": "999"},
        )
    )

    # 复搬时过滤门看到的是同一个外层事件 → 键必须相同
    key = gate._event_key(outer_event)
    assert key == f"{OUTER_GROUP}:{OUTER_MID}"
    # 放行登记已经登记过（一次性，_approved_now 会消费掉它）
    assert key in gate._approved
