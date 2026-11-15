"""图片过滤门接入搬运主流程后的行为（ForwardService 层）。

锁住三件事：

1. 单图命中 → 标签发审计群、**不搬给普通目标**（``block_matched``）；
2. ``block_matched=false`` → 标签照发，搬运照旧（只审计不拦截）；
3. 聊天记录里的图命中 → 记录发审计群打标签，返回 0（不搬给普通目标）；
   打标签抛异常不能反过来把搬运流程打挂。
"""

from __future__ import annotations

import asyncio

from bot.services.forward import ForwardService
from bot.services.image_dedup import ForwardImageStore
from messaging.image_filter import FilterHit, ImageProbe


# ── 假的过滤门：只关心接口行为，不真的下载 / 发送 ──
class _FakeGate:
    def __init__(self, *, hits=None, block=True, scan_all=True, publish_error=False):
        self._hits = list(hits or [])
        self._block = block
        self._scan_all = scan_all
        self._publish_error = publish_error
        self.published_events: list = []
        self.published_records: list = []
        self.noted: list = []
        self.screened_events: list = []
        self.screened_messages: list = []

    @property
    def config(self) -> dict:
        return {"block_matched": self._block, "scan_all_images": self._scan_all}

    @property
    def enabled(self) -> bool:
        return True

    def note_event(self, event) -> None:
        self.noted.append(event)

    async def screen_event(self, event) -> list:
        self.screened_events.append(event)
        return list(self._hits)

    async def screen_messages(self, messages, *, event=None) -> list:
        self.screened_messages.append(list(messages or []))
        return list(self._hits)

    async def publish_event(self, event, hits) -> None:
        if self._publish_error:
            raise RuntimeError("审计群发不出去")
        self.published_events.append((event, hits))

    async def publish_record(self, event, hits, *, nodes=None) -> None:
        if self._publish_error:
            raise RuntimeError("审计群发不出去")
        self.published_records.append((event, hits, nodes))

    def load_image(self, url, timeout=8.0):
        return None


class _FakeMP:
    """机器人图片路径 + 聊天记录路径各给最小判定。"""

    def __init__(self) -> None:
        self.images: list = []

    def is_contain_image(self, _event) -> bool:
        return True

    def check_is_miniapp(self, _event) -> dict:
        return {}

    def check_is_old_forward(self, event) -> bool:
        return any(seg.get("type") == "forward" for seg in event.get("message") or [])

    def check_is_multimsg_forward(self, _event):
        return None

    async def ensure_connected(self, *a, **kw):
        return True

    async def send_img(self, **kw):
        self.images.append(kw)


class _FakeAPI:
    def __init__(self, messages=None) -> None:
        self.calls: list = []
        self._messages = list(messages or [])

    async def get_forward_msg(self, params):
        return {"status": "ok", "data": {"messages": list(self._messages)}}

    async def send_group_forward_msg(self, group_id, messages, timeout=None):
        self.calls.append(("group_forward", str(group_id), len(messages)))
        return {"status": "ok", "data": {"message_id": 8001}}

    async def send_private_forward_msg(self, user_id, messages, timeout=None):
        self.calls.append(("private_forward", str(user_id), len(messages)))
        return {"status": "ok", "data": {"message_id": 8002}}

    async def forward_group_single_msg(self, group_id, message_id):
        self.calls.append(("card_forward", str(group_id), message_id))
        return {"status": "ok", "data": {"message_id": 7001}}


BOT_ID = 1013098110
WHITELIST = {
    "enable": True,
    "bot_list": [{"bot_id": BOT_ID, "type": "all", "group_id": [], "user_id_list": []}],
}
TARGETS = [1079845768]


def _hit(message_id="900", manual=False) -> FilterHit:
    return FilterHit(
        filter_id="phoebe_bullshit",
        label="菲比暴论",
        probe=ImageProbe(
            url="https://cdn/a.png",
            group_id="949950852",
            sender_id=str(BOT_ID),
            message_id=message_id,
        ),
        detail="command:菲比暴论",
        manual=manual,
    )


# ══════════════════ 嵌套记录：过滤门必须看到叶子消息 ══════════════════
# 回归自 2026-11-11 群 1128873261 的真实漏判：SnowLuma 的 get_forward_msg
# 只给一层，嵌套记录在这一层全是 type=forward 段。以前过滤门把这一层直接
# 交给 screen_messages，9 张记录里只看到 2 张图（实际 512 张），其余图片
# 一张没判就搬进了目标群。这里锁住「过滤门拿到的是摊平后的叶子消息」。
def _nested_record_event() -> dict:
    return {
        "message_type": "group",
        "group_id": 949950852,
        "user_id": 10003,
        "message_id": 64035048,
        "message": [{"type": "forward", "data": {"id": "outer"}}],
    }


SINGLE_LAYER = [
    {"user_id": 10003, "message_id": 11, "message": [{"type": "text", "data": {"text": "外层"}}]},
    {"user_id": 10004, "message_id": 12, "message": [{"type": "forward", "data": {"id": "inner"}}]},
]
LEAF_MESSAGES = [
    {"user_id": 10003, "message_id": 21, "message": [{"type": "text", "data": {"text": "内层"}}]},
    {
        "user_id": 10004,
        "message_id": 22,
        "message": [{"type": "image", "data": {"file": "n.png", "url": "https://cdn/n.png"}}],
    },
]


def test_record_filter_screens_expanded_leaves_not_single_layer(monkeypatch):
    """过滤门必须扫摊平后的叶子消息，而不是单层卡片内容。"""
    gate = _FakeGate(hits=[], block=True)
    service, api = _record_service(gate, messages=SINGLE_LAYER)

    async def fake_expand(event, kind="old_forward", *, max_items=200):
        assert max_items >= 1000, "过滤要求整份摊平，不能被去重的 200 条上限截断"
        return list(LEAF_MESSAGES), "expand[测试]"

    monkeypatch.setattr(service, "_expand_forward_for_dedup", fake_expand)

    asyncio.run(
        service.forward_rebuilt(
            _nested_record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert len(gate.screened_messages) == 1
    screened = gate.screened_messages[0]
    assert screened == LEAF_MESSAGES, "过滤门拿到的必须是叶子消息（含真实图片段）"
    assert any(
        seg.get("type") == "image"
        for msg in screened
        for seg in msg.get("message") or []
    )


def test_record_filter_falls_back_to_single_layer_when_expand_empty(monkeypatch):
    """展开拿不到内容时退回单层扫描——宁可少判，也不能完全不判。"""
    gate = _FakeGate(hits=[], block=True)
    service, api = _record_service(gate, messages=SINGLE_LAYER)

    async def fake_expand(event, kind="old_forward", *, max_items=200):
        return [], "expand[空]"

    monkeypatch.setattr(service, "_expand_forward_for_dedup", fake_expand)

    asyncio.run(
        service.forward_rebuilt(
            _nested_record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert gate.screened_messages == [SINGLE_LAYER]


def test_record_filter_blocks_when_hit_only_visible_after_expand(monkeypatch):
    """只在叶子层出现的色图命中 → 必须拦截（这正是线上漏判的场景）。"""
    gate = _FakeGate(hits=[_hit(message_id="22")], block=True)
    service, api = _record_service(gate, messages=SINGLE_LAYER)

    async def fake_expand(event, kind="old_forward", *, max_items=200):
        return list(LEAF_MESSAGES), "expand[测试]"

    monkeypatch.setattr(service, "_expand_forward_for_dedup", fake_expand)

    result = asyncio.run(
        service.forward_rebuilt(
            _nested_record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert result == 0, "叶子层命中的色图必须拦住整条记录"
    assert len(gate.published_records) == 1
    assert api.calls == []


def _image_event() -> dict:
    return {
        "message_type": "group",
        "user_id": BOT_ID,
        "group_id": 949950852,
        "message_id": 1652911697,
        "message": [{"type": "image", "data": {"file": "abc.image", "url": "https://cdn/a.png"}}],
    }


def _service(monkeypatch, gate: _FakeGate) -> tuple[ForwardService, list]:
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=None,
        image_store=ForwardImageStore(db_path=":memory:"),
        image_gate=gate,
    )
    sent: list = []

    async def fake_batch(event, target_groupid=None, task_userid=None):
        sent.append((event.get("message_type"), event.get("user_id")))
        return len(target_groupid or [])

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)
    return service, sent


def _run(service, event) -> bool:
    return asyncio.run(
        service.auto_forward(
            event=event,
            groupid_list=[949950852],
            userid_list=[],
            target_groupid=TARGETS,
            task_userid=[],
            auto_img_forward_from_bot=WHITELIST,
        )
    )


# ══════════════════ 单图：命中 → 只发审计标签，不搬 ══════════════════
def test_single_image_hit_blocks_normal_targets(monkeypatch):
    gate = _FakeGate(hits=[_hit()], block=True)
    service, sent = _service(monkeypatch, gate)
    event = _image_event()

    assert _run(service, event) is True
    # 事件先被记进最近命令追踪（菲比暴论这类「先命令后出图」要靠它）
    assert gate.noted == [event]
    assert len(gate.published_events) == 1, "命中后必须把标签发到审计群"
    assert sent == [], "命中 block_matched 的图不能再搬给普通目标"


def test_single_image_hit_without_block_still_forwards(monkeypatch):
    """block_matched=false：标签照发（纯审计），搬运照旧。"""
    gate = _FakeGate(hits=[_hit()], block=False)
    service, sent = _service(monkeypatch, gate)

    assert _run(service, _image_event()) is True
    assert len(gate.published_events) == 1
    assert sent == [("group", BOT_ID)]


def test_single_image_publish_error_does_not_break_blocking(monkeypatch):
    """审计群发不出去（异常）也要拦住，不能把命中图搬给普通目标。"""
    gate = _FakeGate(hits=[_hit()], block=True, publish_error=True)
    service, sent = _service(monkeypatch, gate)

    assert _run(service, _image_event()) is True
    assert gate.published_events == []
    assert sent == [], "打标签失败不能放行命中图"


def test_single_image_miss_forwards_normally(monkeypatch):
    gate = _FakeGate(hits=[], block=True)
    service, sent = _service(monkeypatch, gate)

    assert _run(service, _image_event()) is True
    assert gate.published_events == []
    assert sent == [("group", BOT_ID)]


# ══════════════════ 聊天记录：命中 → 记录发审计群，不搬 ══════════════════
def _record_service(gate: _FakeGate, messages=None) -> tuple[ForwardService, _FakeAPI]:
    api = _FakeAPI(messages=messages)
    service = ForwardService(mp=_FakeMP(), api=api, settings=None, image_gate=gate)
    return service, api


def _record_event() -> dict:
    return {
        "message_type": "group",
        "group_id": 949950852,
        "user_id": 10003,
        "message_id": 64035048,
        "message": [{"type": "forward", "data": {"id": "7700155396542626315"}}],
    }


def _record_messages() -> list[dict]:
    return [
        {
            "user_id": 10003,
            "time": 1792736000,
            "message_id": 1,
            "sender": {"user_id": 10003, "nickname": "甲", "card": ""},
            "message": [{"type": "text", "data": {"text": "看图"}}],
        },
        {
            "user_id": 10004,
            "time": 1792736001,
            "message_id": 2,
            "sender": {"user_id": 10004, "nickname": "乙", "card": ""},
            "message": [{"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}}],
        },
    ]


def test_record_hit_blocks_and_forwards_to_audit(monkeypatch):
    gate = _FakeGate(hits=[_hit(message_id="2")], block=True)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert result == 0, "命中 block_matched 的记录不能再搬给普通目标"
    assert len(gate.published_records) == 1
    event, hits, nodes = gate.published_records[0]
    assert hits[0].filter_id == "phoebe_bullshit"
    assert nodes, "审计群要拿到重建好的节点，别只发标签看不到内容"
    assert api.calls == [], "普通目标一个都不能发"


def test_record_hit_without_block_falls_back_to_whole_card(monkeypatch):
    """block_matched=false：标签照发，搬运不拦（退回整卡转发）。"""
    gate = _FakeGate(hits=[_hit(message_id="2")], block=False)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    # 没开裁剪、没追加 → 退回整卡转发（None），但标签必须已经发过
    assert result is None
    assert len(gate.published_records) == 1
    assert api.calls == [], "tail 关着时这里不发重建消息，整卡转发交给调用方"


def test_record_publish_error_still_blocks(monkeypatch):
    gate = _FakeGate(hits=[_hit(message_id="2")], block=True, publish_error=True)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert result == 0, "打标签失败也不能放行命中记录"
    assert gate.published_records == []
    assert api.calls == []


def test_record_miss_falls_back_to_whole_card(monkeypatch):
    gate = _FakeGate(hits=[], block=True)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert result is None, "没命中也没有裁剪 → 照旧整卡转发"
    assert gate.published_records == []
    assert api.calls == []


def test_record_gate_disabled_skips_scan(monkeypatch):
    """scan_all_images=false：聊天记录里的图不扫描，照旧整卡转发。"""
    gate = _FakeGate(hits=[_hit(message_id="2")], block=True, scan_all=False)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert result is None
    assert gate.screened_messages == []
    assert gate.published_records == []


def test_all_manual_hits_do_not_block(monkeypatch):
    """黑白图待人工（manual）命中：审计照发，但照常搬运给普通目标。"""
    gate = _FakeGate(hits=[_hit(message_id="2", manual=True)], block=True)
    service, api = _record_service(gate, messages=_record_messages())

    result = asyncio.run(
        service.forward_rebuilt(
            _record_event(), opts=None, target_groupid=[1079845768], task_userid=[]
        )
    )

    assert len(gate.published_records) == 1, "manual 命中也要发审计"
    assert result is None, "全 manual 命中不应 return 0（拦截），应退回整卡转发"
