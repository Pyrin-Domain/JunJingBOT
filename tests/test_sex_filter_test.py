"""``junjingtest_sex_filter`` 命令：定位目标、取内容、全量判定、报告形状。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest

import bot.commands.sex_filter_test as mod
from messaging.image_filter import ImageFilterHub


def _layout_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1800, 600), (110, 80, 100))
    draw = ImageDraw.Draw(image)
    draw.rectangle([900, 0, 1800, 600], fill=(0, 0, 0))
    for y in range(80, 500, 60):
        draw.rectangle([980, y, 1720, y + 24], fill=(255, 255, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _record_messages() -> list[dict]:
    return [
        {
            "user_id": 10003,
            "message_id": 1,
            "time": 1792736000,
            "sender": {"user_id": 10003, "nickname": "甲", "card": "卡甲"},
            "message": [{"type": "text", "data": {"text": "看图"}}],
        },
        {
            "user_id": 3792417235,
            "message_id": 2,
            "time": 1792736001,
            "group_id": 740726382,
            "sender": {"user_id": 3792417235, "nickname": "菲比大人", "card": ""},
            "message": [
                {"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}}
            ],
        },
    ]


class _FakeAPI:
    def __init__(self, *, replied=None, record=None, history=None):
        self.replied = replied
        self.record = list(record or [])
        self.history = list(history or [])
        self.sent: list = []
        self.forward_calls: list = []

    async def get_message(self, message_id):
        return {"status": "ok", "data": self.replied} if self.replied else {"data": None}

    async def get_forward_msg(self, params):
        self.forward_calls.append(params.get("message_id"))
        if self.record:
            return {"status": "ok", "data": {"messages": list(self.record)}}
        return {"status": "failed", "data": {"messages": []}}

    async def get_group_msg_history(self, group_id, count=20, reverse_order=False, **kw):
        return {"status": "ok", "data": {"messages": list(self.history)}}

    async def send_group_message(self, group_id, message, timeout=None):
        self.sent.append((str(group_id), message))
        return {"status": "ok", "data": {"message_id": 9001}}


class _FakeMP:
    def is_reply(self, event):
        for seg in event.get("message") or []:
            if seg.get("type") == "reply":
                return seg["data"]["id"]
        return None

    async def ensure_connected(self, *a, **kw):
        return True


def _handler(api: _FakeAPI, hub: ImageFilterHub):
    forward = SimpleNamespace(image_gate=SimpleNamespace(hub=hub))
    return SimpleNamespace(mp=_FakeMP(), api=api, forward=forward, settings=None)


def _hub(settings=None) -> ImageFilterHub:
    return ImageFilterHub(
        settings=settings
        or {
            "image_filter": {
                "enable": True,
                "max_images": 12,
                "filters": [
                    {
                        "id": "phoebe_bullshit",
                        "enable": True,
                        "params": {
                            # 注意：空列表会回退到过滤函数内置的默认白名单，
                            # 测试里显式列出要放行的群 / 发送者
                            "sender_ids": [3792417235],
                            "group_ids": [740726382, 1079845768],
                            "command": "菲比暴论",
                            "command_window": 120,
                        },
                    }
                ],
            }
        },
        download=lambda url, timeout=8: _layout_png(),
    )


def _command_event(reply_id=None) -> dict:
    event = {
        "message_type": "group",
        "group_id": 1079845768,
        "user_id": 1013098110,
        "message_id": 600,
        "message": [{"type": "text", "data": {"text": "junjingtest_sex_filter"}}],
    }
    if reply_id is not None:
        event["message"].insert(0, {"type": "reply", "data": {"id": reply_id}})
    return event


@pytest.fixture(autouse=True)
def _no_nsfw_score(monkeypatch):
    monkeypatch.setattr(mod, "nsfw_score_bytes", lambda data, params: None)


def test_reply_mode_runs_all_filters_and_reports(monkeypatch):
    hub = _hub()
    hub.commands.note_event(
        {"group_id": 740726382, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]}
    )
    api = _FakeAPI(
        replied={"message_id": 500, "message": [{"type": "forward", "data": {"id": "x"}}]},
        record=_record_messages(),
    )
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    assert api.forward_calls == [500], "只按被回复消息的 id 取内容"
    assert len(api.sent) == 2, "报告 1 条 + 回显 1 条"
    group_id, message = api.sent[0]
    assert group_id == "1079845768"
    assert message[0]["type"] == "reply" and message[0]["data"]["id"] == "600"
    text = message[1]["data"]["text"]
    assert "【图片过滤测试】" in text
    assert "回复的消息 500" in text
    # 菲比暴论：同群最近命令 + 发送者不限 → 命中
    assert "菲比暴论（phoebe_bullshit）" in text
    assert "命中 1 张" in text


def test_no_reply_falls_back_to_latest_record_in_history(monkeypatch):
    hub = _hub()
    history = [
        {
            "message_id": 410,
            "user_id": 10003,
            "message": [{"type": "text", "data": {"text": "普通聊天"}}],
        },
        {
            "message_id": 420,
            "user_id": 10003,
            "message": [{"type": "forward", "data": {"id": "y"}}],
        },
    ]
    api = _FakeAPI(record=_record_messages(), history=history)
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event()))

    # 跳过普通聊天，命中往上数最近的一条聊天记录 420
    assert api.forward_calls == [420]
    text = api.sent[0][1][1]["data"]["text"]
    assert "往上数最近的一条聊天记录" in text and "420" in text


def test_usage_hint_when_nothing_to_judge(monkeypatch):
    api = _FakeAPI()
    handler = _handler(api, _hub())

    asyncio.run(mod.sex_filter_test(handler, _command_event()))

    text = api.sent[0][1][1]["data"]["text"]
    assert "用法" in text


def test_record_without_images_reports_nothing_to_scan(monkeypatch):
    hub = _hub()
    api = _FakeAPI(
        replied={"message_id": 500, "message": [{"type": "forward", "data": {"id": "x"}}]},
        record=[_record_messages()[0]],  # 只有文本
    )
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    text = api.sent[0][1][1]["data"]["text"]
    assert "没有图片" in text


def test_replied_single_image_message_is_judged_directly(monkeypatch):
    """回复的是单张图（不是聊天记录）→ 直接判这张图。"""
    hub = _hub()
    hub.commands.note_event(
        {"group_id": 1079845768, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]}
    )
    api = _FakeAPI(
        replied={
            "message_id": 510,
            "user_id": 3792417235,
            "group_id": 1079845768,
            "sender": {"user_id": 3792417235, "nickname": "菲比大人", "card": ""},
            "message": [
                {"type": "image", "data": {"file": "a.png", "url": "https://cdn/a.png"}}
            ],
        },
        record=[],  # get_forward_msg 失败 → 退回单条消息判定
    )
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=510)))

    text = api.sent[0][1][1]["data"]["text"]
    assert "菲比暴论（phoebe_bullshit）" in text
    assert "目标本身带图" in text


# ════════════════════ 嵌套记录：抽象层展平 ════════════════════
class _FakeKernel:
    """ForwardExpander 需要的最小 kernel：call() 返回 .ok/.messages/.error。"""

    def __init__(self, tree):
        self.tree = tree
        self.calls: list = []
        self.capabilities = SimpleNamespace(forward_inner_id_fetchable=True)

    async def call(self, action, params):
        self.calls.append((action, params.get("message_id")))
        msgs = self.tree.get(str(params.get("message_id")))
        return SimpleNamespace(ok=msgs is not None, messages=list(msgs or []), error=None)


def _leaf_image(url="https://cdn/inner.png", uid=3792417235, name="菲比大人"):
    return {
        "user_id": uid,
        "message_id": 2,
        "group_id": 740726382,
        "sender": {"user_id": uid, "nickname": name, "card": ""},
        "message": [{"type": "image", "data": {"file": "a.png", "url": url}}],
    }


def test_nested_record_expands_to_leaf_images(monkeypatch):
    """SnowLuma 嵌套记录：单层全是 forward 段，必须走抽象层展开才看得到图。"""
    hub = _hub()
    hub.commands.note_event(
        {"group_id": 740726382, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]}
    )
    tree = {
        "root": [
            {"user_id": 1, "message_id": 1, "sender": {"user_id": 1, "nickname": "甲"},
             "message": [{"type": "text", "data": {"text": "看这个"}}]},
            {"user_id": 1, "message_id": 2, "sender": {"user_id": 1, "nickname": "甲"},
             "message": [{"type": "forward", "data": {"id": "inner"}}]},
        ],
        "inner": [_leaf_image()],
    }
    api = _FakeAPI()   # get_forward_msg 没配数据：单层路径拿不到东西
    api.kernel = _FakeKernel(tree)
    api.replied = {"message_id": 500, "message": [{"type": "forward", "data": {"id": "root"}}]}
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    text = api.sent[0][1][1]["data"]["text"]
    assert api.forward_calls == [], "展开成功就不该再走单层 get_forward_msg"
    assert "expand[" in text
    assert "菲比暴论（phoebe_bullshit）" in text, "内层的图要被判到"


def test_expand_failure_falls_back_to_single_level(monkeypatch):
    """展开不出内容 → 退回单层 get_forward_msg（NapCat 内联场景）。"""
    hub = _hub()
    hub.commands.note_event(
        {"group_id": 740726382, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]}
    )
    api = _FakeAPI(record=_record_messages())
    api.kernel = _FakeKernel({"missing": []})   # expand_event(root) 拿不到
    api.replied = {"message_id": 500, "message": [{"type": "forward", "data": {"id": "root"}}]}
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    text = api.sent[0][1][1]["data"]["text"]
    assert api.forward_calls == [500], "展开失败要退回单层查询"
    assert "get_forward_msg[" in text
    assert "菲比暴论（phoebe_bullshit）" in text


def test_command_error_reports_back(monkeypatch):
    """命令任何一步抛异常都要回反馈——测试命令不能无声失败。"""
    hub = _hub()
    api = _FakeAPI(replied={"message_id": 500, "message": [{"type": "forward", "data": {"id": "x"}}]})
    handler = _handler(api, hub)

    async def boom(_handler, _event):
        raise RuntimeError("下载服务炸了")

    monkeypatch.setattr(mod, "_run_test", boom)
    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    assert api.sent, "出错也必须有反馈消息"
    text = api.sent[0][1][1]["data"]["text"]
    assert "命令执行出错" in text and "RuntimeError: 下载服务炸了" in text


def test_botapp_action_registry_includes_sex_filter_test():
    """防回归：运行时的 action 表是 BotApp.action_registry（手写 dict），
    新命令只登记 bot/commands/registry.py 不够，漏了 app.py 就「未知 action」。"""
    from types import SimpleNamespace

    from bot.app import BotApp

    app_like = SimpleNamespace(
        mp=SimpleNamespace(
            send_message=None,
            send_img=None,
            get_long_history_test=None,
            set_tokenizer=None,
            ocr=None,
            try_send_bili_video=None,
            try_send_reply_bili_card=None,
        ),
        commands=SimpleNamespace(
            export=None,
            recall=None,
            fabric=None,
            get_music=None,
            img_forward_create_task=None,
            jm=None,
            sex_filter_test=lambda event: None,
        ),
    )
    registry = BotApp.action_registry(app_like)
    assert callable(registry.get("sex_filter_test")), (
        "BotApp.action_registry 缺 sex_filter_test：新命令要在 app.py 的手写表里也登记"
    )


# ══════════════════ 判定回显：[{text}，{image}] ══════════════════
def test_echo_images_sent_with_verdict(monkeypatch):
    """判定后每张图回显一条 [text, image]，文本写明结论。"""
    monkeypatch.setattr(mod, "_ECHO_INTERVAL", 0)
    hub = _hub()
    hub.commands.note_event(
        {"group_id": 740726382, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]}
    )
    api = _FakeAPI(record=_record_messages())
    api.replied = {"message_id": 500, "message": [{"type": "forward", "data": {"id": "root"}}]}
    handler = _handler(api, hub)

    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    # sent[0] = 报告（reply + text）；后面是每张判定图的回显
    assert len(api.sent) == 2, f"报告 1 条 + 回显 1 条，实际 {len(api.sent)}"
    report_msg, echo_msg = api.sent[0][1], api.sent[1][1]
    assert echo_msg[0]["type"] == "text"
    assert echo_msg[1]["type"] == "image"
    assert echo_msg[1]["data"]["file"].startswith("base64://")
    echo_text = echo_msg[0]["data"]["text"]
    assert echo_text.startswith("#2 菲比大人（3792417235）：")
    assert "phoebe_bullshit" in echo_text, "命中结论要进回显文本"


def test_echo_text_says_not_nsfw_when_scored(monkeypatch):
    """nsfw 打分可用时，未命中的图回显「不是色图（nsfw=… ok）」。"""
    monkeypatch.setattr(mod, "_ECHO_INTERVAL", 0)

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {"score": 0.01, "verdict": "ok", "worst_view": "full"}

    import messaging.image_filter as img_mod

    monkeypatch.setattr(
        img_mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: _FakeScorer(),
    )
    # autouse fixture 把 mod.nsfw_score_bytes 换成了 None 桩，这里换回真函数
    from messaging.image_filter import nsfw_score_bytes as real_score

    monkeypatch.setattr(mod, "nsfw_score_bytes", real_score)
    hub = _hub({
        "image_filter": {
            "enable": True,
            "max_images": 12,
            "filters": [
                {"id": "phoebe_bullshit", "enable": True, "params": {
                    # 显式排除测试记录的群 → 菲比不命中，走 nsfw 结论路径
                    "sender_ids": [3792417235], "group_ids": [888888],
                    "command": "菲比暴论", "command_window": 120}},
                {"id": "nsfw", "enable": True, "params": {
                    "threshold": 0.5, "model_path": "D:/fake/model.onnx"}},
            ],
        }
    })
    api = _FakeAPI(record=_record_messages())
    api.replied = {"message_id": 500, "message": [{"type": "forward", "data": {"id": "root"}}]}
    handler = _handler(api, hub)

    print("DEBUG params_by_id:", mod._filter_params(hub))
    asyncio.run(mod.sex_filter_test(handler, _command_event(reply_id=500)))

    echo_text = api.sent[1][1][0]["data"]["text"]
