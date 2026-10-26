"""搬运链路接入「末尾广告裁剪 + 尾部追加」后的行为。

重点是三条：
1. 功能关着 → 一条路都不变，还是整卡转发（不能因为新功能改变老行为）；
2. 功能开着且末尾有广告 → 走重建发送，条数变少、追加的在最后；
3. 任何一步失败（取不到内容 / 全发失败）→ 退回整卡转发，别把消息卡住。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from bot.services.forward import ForwardService
from messaging import forward_filter as ff


# ── 夹具 ──────────────────────────────────────────────
def _msg(text=None, image_url=None, uid=10003, nick="QQ用户"):
    seg = (
        {"type": "text", "data": {"text": text}}
        if text is not None
        else {"type": "image", "data": {"file": "x.png", "url": image_url}}
    )
    return {
        "user_id": uid,
        "time": 1792736000,
        "message_id": 1,
        "sender": {"user_id": uid, "nickname": nick, "card": ""},
        "message": [seg],
    }


def _group_card():
    payload = {
        "app": "com.tencent.tuwen.lua",
        "prompt": "欢迎加入艺术站【点我传送👋】",
        "meta": {"news": {"title": "欢迎加入艺术站", "desc": "点我加入", "jumpUrl": "https://m.q.qq.com/a/p/1"}},
    }
    return {
        "user_id": 10003,
        "time": 1792736492,
        "message_id": 2,
        "sender": {"user_id": 10003, "nickname": "QQ用户", "card": ""},
        "message": [{"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}],
    }


FORWARD_EVENT = {
    "post_type": "message",
    "message_type": "group",
    "group_id": 20002,
    "user_id": 10003,
    "message_id": 64035048,
    "message": [{"type": "forward", "data": {"id": "7700155396542626315"}}],
}


class _FakeMP:
    def __init__(self):
        self.images = []

    def check_is_old_forward(self, event):
        return any(seg.get("type") == "forward" for seg in event.get("message") or [])

    def check_is_miniapp(self, event):
        return {}

    async def ensure_connected(self, *a, **kw):
        return True

    async def send_img(self, **kw):
        self.images.append(kw)


class _FakeAPI:
    def __init__(self, *, forward_ok=True, timeout=False):
        self.calls = []
        self.forward_ok = forward_ok
        self.timeout = timeout

    async def get_forward_msg(self, params):
        return {"status": "ok", "data": {"messages": list(_FakeAPI.messages)}}

    async def send_group_forward_msg(self, group_id, messages, timeout=None):
        self.calls.append(("group_forward", group_id, len(messages)))
        if self.timeout:
            raise TimeoutError("没有回执")
        if not self.forward_ok:
            return {"status": "failed", "message": "发送失败"}
        return {"status": "ok", "data": {"message_id": 1}}

    async def send_private_forward_msg(self, user_id, messages, timeout=None):
        self.calls.append(("private_forward", user_id, len(messages)))
        return {"status": "ok", "data": {"message_id": 2}}

    async def forward_group_single_msg(self, group_id, message_id):
        self.calls.append(("card_forward", group_id, message_id))
        return {"status": "ok"}

    async def forward_friend_single_msg(self, user_id, message_id):
        self.calls.append(("card_forward_friend", user_id, message_id))
        return {"status": "ok"}


def _service(*, messages=None, forward_ok=True, timeout=False):
    api = _FakeAPI(forward_ok=forward_ok, timeout=timeout)
    _FakeAPI.messages = list(messages if messages is not None else [])
    mp = _FakeMP()
    svc = ForwardService(mp=mp, api=api, settings=SimpleNamespace(
        get=lambda key, default=None: default,
        reply=lambda key, default="": default,
    ))
    return svc, api


@pytest.fixture(autouse=True)
def no_real_download(monkeypatch):
    """测试里不真去下载图片。"""
    monkeypatch.setattr(ff, "download_image", lambda url, timeout=0: None)


def _opts(**kw):
    base = {"enable": True, "scan_tail": 3, "drop_qr": True, "drop_group_card": True, "drop_link": True,
            "append": {"enable": False, "items": []}}
    base.update(kw)
    return ff.normalize_settings(base)


# ══════════════════ forward_rebuilt ══════════════════
def test_disabled_returns_none():
    svc, api = _service(messages=[_msg("A")])
    assert asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=ff.normalize_settings(None))) is None
    assert asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=ff.normalize_settings({"enable": False}))) is None
    assert api.calls == []


def test_nothing_to_do_returns_none():
    svc, api = _service(messages=[_msg("A"), _msg("B"), _msg("C")])
    assert asyncio.run(
        svc.forward_rebuilt(FORWARD_EVENT, opts=_opts(), target_groupid=[20003])
    ) is None
    assert api.calls == []


def test_strip_and_append_sends_rebuilt():
    messages = [_msg("正文"), _msg(image_url="https://cdn/a.png"), _msg(image_url="https://cdn/qr.png"), _group_card()]
    svc, api = _service(messages=messages)
    opts = _opts(append={"enable": True, "items": [{"uin": 10004, "name": "签名号", "content": "独角兽君景搬运喵~"}]})
    sent = asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=opts, target_groupid=[20003]))
    assert sent == 1
    kind, group_id, count = api.calls[0]
    assert kind == "group_forward" and group_id == 20003
    # 4 条 → 丢掉群卡片 1 条，再追加 1 条 = 4
    assert count == 4


def test_skips_source_group():
    messages = [_msg("正文"), _group_card()]
    svc, api = _service(messages=messages)
    opts = _opts(append={"enable": True, "items": [{"content": "签名"}]})
    sent = asyncio.run(
        svc.forward_rebuilt(
            FORWARD_EVENT, opts=opts, target_groupid=[20002, 20003]
        )
    )
    assert sent == 1
    assert [call[1] for call in api.calls] == [20003]


def test_private_target_uses_private_forward():
    messages = [_msg("正文"), _group_card()]
    svc, api = _service(messages=messages)
    opts = _opts(append={"enable": True, "items": [{"content": "签名"}]})
    sent = asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=opts, task_userid=[10001]))
    assert sent == 1
    assert api.calls[0][0] == "private_forward"


def test_all_sends_failed_falls_back():
    messages = [_msg("正文"), _group_card()]
    svc, api = _service(messages=messages, forward_ok=False)
    opts = _opts(append={"enable": True, "items": [{"content": "签名"}]})
    assert asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=opts, target_groupid=[20003])) is None


def test_timeout_is_treated_as_sent_not_fallback():
    """超时 ≠ 失败：NapCat 往往已经发出去了，再退回整卡转发就会重复搬运。"""
    messages = [_msg("正文"), _group_card()]
    svc, api = _service(messages=messages, timeout=True)
    opts = _opts(append={"enable": True, "items": [{"content": "签名"}]})
    sent = asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=opts, target_groupid=[20003]))
    assert sent == 1
    assert [call[0] for call in api.calls] == ["group_forward"]


def test_empty_forward_content_falls_back():
    svc, api = _service(messages=[])
    opts = _opts(append={"enable": True, "items": [{"content": "签名"}]})
    assert asyncio.run(svc.forward_rebuilt(FORWARD_EVENT, opts=opts, target_groupid=[20003])) is None
    assert api.calls == []


# ══════════════════ auto_forward 整体行为 ══════════════════
def test_auto_forward_falls_back_to_card_when_disabled():
    svc, api = _service(messages=[_msg("A"), _group_card()])
    ok = asyncio.run(
        svc.auto_forward(
            event=FORWARD_EVENT,
            groupid_list=[20002],
            target_groupid=[20003],
            strip_tail_ads=None,
        )
    )
    assert ok is True
    assert api.calls == [("card_forward", 20003, 64035048)]


def test_auto_forward_uses_rebuilt_when_enabled():
    svc, api = _service(messages=[_msg("正文"), _group_card()])
    ok = asyncio.run(
        svc.auto_forward(
            event=FORWARD_EVENT,
            groupid_list=[20002],
            target_groupid=[20003],
            strip_tail_ads={"enable": True, "scan_tail": 3, "append": {"enable": False, "items": []}},
        )
    )
    assert ok is True
    assert api.calls[0][0] == "group_forward"
    assert api.calls[0][2] == 1          # 2 条里丢掉群卡片，只剩正文


def test_auto_forward_falls_back_when_content_unavailable():
    svc, api = _service(messages=[])
    ok = asyncio.run(
        svc.auto_forward(
            event=FORWARD_EVENT,
            groupid_list=[20002],
            target_groupid=[20003],
            strip_tail_ads={"enable": True, "append": {"enable": True, "items": [{"content": "签名"}]}},
        )
    )
    assert ok is True
    assert api.calls == [("card_forward", 20003, 64035048)]
