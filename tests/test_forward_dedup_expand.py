"""去重闸门取转发内容时必须走抽象层，不能只取一层。

线上事故：SnowLuma 的 ``get_forward_msg`` 只返回一层，嵌套聊天记录只给内层
``forward id``；旧代码把这一层直接拼指纹，于是两条 ``type=forward`` 被规整成
``[图片]``，图片双哈希去取图时 ``raw_url_cnt=0`` → ``no_image_url``。

这里锁住三件事：

1. SnowLuma 必须拿内层 id 继续展开，NapCat 必须用内联 content（不拿内层 id 发包）；
2. 两个后端最终拿到同一批叶子消息，图片 URL 能被 ``_collect_image_urls`` 收到；
3. 没有 kernel 的旧 API（测试 / 降级环境）仍然退回原来的单层取法。
"""
import asyncio
import json
from types import SimpleNamespace

from AbstractionLayer import CallableTransport, OneBotKernel
from AbstractionLayer.kernal import NAPCAT, SNOWLUMA
from bot.services.forward import ForwardService


EVENT = {
    "message_id": 1,
    "message_type": "group",
    "group_id": 20001,
    "user_id": 9,
    "raw_message": "[CQ:forward,id=top]",
    "message": [{"type": "forward", "data": {"id": "top"}}],
}

LEAVES = [
    {
        "message_id": 9000 + i,
        "sender": {"user_id": 7, "nickname": "叶子"},
        "raw_message": "[CQ:image,file=%d.png]" % i,
        "message": [
            {
                "type": "image",
                "data": {"file": "%d.png" % i, "url": "https://example.test/%d.png" % i},
            }
        ],
    }
    for i in range(2)
] + [
    {
        "message_id": 9100,
        "sender": {"user_id": 7, "nickname": "叶子"},
        "raw_message": "正文",
        "message": [{"type": "text", "data": {"text": "正文"}}],
    }
]


class _Backend:
    """复刻两个后端的嵌套转发差异。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action: str, params: dict) -> dict:
        self.calls.append((action, dict(params)))
        if action != "get_forward_msg":
            return {"status": "failed", "retcode": 400, "message": action}
        forward_id = str(params.get("message_id"))
        if forward_id in ("inner", "multires"):
            return {"status": "ok", "retcode": 0, "data": {"messages": LEAVES}}
        nested = {"type": "forward", "data": {"id": "inner"}}
        if self.mode == "napcat":
            nested["data"]["content"] = LEAVES  # NapCat 内联
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"messages": [{"message_id": 2, "message": [nested]}]},
        }


def _service(mode: str):
    caps = NAPCAT if mode == "napcat" else SNOWLUMA
    backend = _Backend(mode)
    kernel = OneBotKernel(CallableTransport(backend.call), capabilities=caps, label=mode)
    svc = ForwardService(
        mp=SimpleNamespace(),
        api=SimpleNamespace(kernel=kernel),
        settings=SimpleNamespace(get=lambda key, default=None: default),
    )
    return svc, backend


def test_snowluma_expands_nested_forward_for_dedup():
    svc, backend = _service("snowluma")
    messages, info = asyncio.run(svc._expand_forward_for_dedup(EVENT))

    assert info.startswith("expand[")
    assert len(messages) == 3
    assert [name for name, _ in backend.calls] == ["get_forward_msg", "get_forward_msg"]

    urls: list[str] = []
    ForwardService._collect_image_urls(messages, urls, 20)
    assert urls == ["https://example.test/0.png", "https://example.test/1.png"]


def test_napcat_uses_inline_content_without_inner_fetch():
    svc, backend = _service("napcat")
    messages, info = asyncio.run(svc._expand_forward_for_dedup(EVENT))

    assert info.startswith("expand[")
    assert len(messages) == 3
    assert [name for name, _ in backend.calls] == ["get_forward_msg"]


def test_multimsg_uses_detail_resid_for_nested_expansion():
    svc, backend = _service("snowluma")
    payload = {
        "app": "com.tencent.multimsg",
        "meta": {"detail": {"resid": "multires"}},
    }
    event = {
        "message_id": 3,
        "message_type": "group",
        "group_id": 20001,
        "message": [
            {
                "type": "json",
                "data": {"data": json.dumps(payload, ensure_ascii=False)},
            }
        ],
    }
    messages, info = asyncio.run(
        svc._expand_forward_for_dedup(event, kind="multimsg")
    )

    assert info.startswith("expand[")
    assert len(messages) == 3
    assert backend.calls[0] == ("get_forward_msg", {"message_id": "multires"})


def test_without_kernel_falls_back_to_single_level_fetch():
    svc = ForwardService(
        mp=SimpleNamespace(),
        api=SimpleNamespace(),  # 旧 API / 测试 fake：没有 kernel
        settings=SimpleNamespace(get=lambda key, default=None: default),
    )

    async def fake_fetch(event, kind="old_forward"):
        return [{"message_id": 1, "raw_message": "单层"}], "get_forward_msg[1:ok/1]"

    svc._fetch_forward_messages = fake_fetch
    messages, info = asyncio.run(svc._expand_forward_for_dedup(EVENT))

    assert messages[0]["raw_message"] == "单层"
    assert info == "get_forward_msg[1:ok/1]|fallback"
