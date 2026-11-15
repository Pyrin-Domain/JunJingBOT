"""``OneBotAPI`` 发送出口的兼容处理。

``file`` 段的 ``name`` 由抽象层补：SnowLuma 缺它会直接在 QQ 的
``UploadGroupFileRequest`` 阶段回 ``-134``，NapCat 则把 ``name`` 当可选
显示名。这里锁住「所有 send_group_msg / send_private_msg 调用方都自动带上」
这个契约。
"""

from __future__ import annotations

import asyncio

import pytest

from transport.api import OneBotAPI


class FakeConnection:
    is_connected = True
    is_reverse = True
    config = None

    def __init__(self, backend="snowluma"):
        self.backend = backend
        self.calls: list[tuple] = []

    async def ensure_connected(self, timeout=None):
        return None

    async def call_api(self, action, params, timeout=None):
        self.calls.append((action, params, timeout))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


def _run(coro):
    return asyncio.run(coro)


def _pdf_segment():
    return {"type": "file", "data": {"file": r"C:\tmp\350234.pdf"}}


@pytest.mark.parametrize("backend", ["snowluma", "napcat"])
def test_group_file_gets_name_at_api_layer(backend):
    conn = FakeConnection(backend)
    api = OneBotAPI(conn)

    _run(api.send_group_message(1000, [_pdf_segment()], timeout=120))

    action, params, timeout = conn.calls[0]
    assert action == "send_group_msg"
    assert timeout == 120
    assert params["group_id"] == 1000
    assert params["message"][0]["data"]["name"] == "350234.pdf"


def test_private_file_gets_name_at_api_layer():
    conn = FakeConnection()
    api = OneBotAPI(conn)

    _run(api.send_private_message(123456, [_pdf_segment()]))

    action, params, _ = conn.calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == 123456
    assert params["message"][0]["data"]["name"] == "350234.pdf"


def test_explicit_name_is_not_overridden():
    conn = FakeConnection()
    api = OneBotAPI(conn)
    segment = {"type": "file", "data": {"file": r"C:\tmp\a.pdf", "name": "报告.pdf"}}

    _run(api.send_group_message(1000, [segment]))

    assert conn.calls[0][1]["message"][0]["data"]["name"] == "报告.pdf"


def test_image_segment_is_untouched():
    conn = FakeConnection()
    api = OneBotAPI(conn)
    segment = {"type": "image", "data": {"file": r"C:\tmp\a.jpg"}}

    _run(api.send_group_message(1000, [segment]))

    assert "name" not in conn.calls[0][1]["message"][0]["data"]


def test_non_message_actions_are_not_rewritten():
    conn = FakeConnection()
    api = OneBotAPI(conn)

    _run(api._call_api("get_group_info", {"group_id": 1000}))

    assert conn.calls[0][1] == {"group_id": 1000}
