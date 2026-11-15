"""有副作用的动作必须校验 ``retcode``，``get_*`` 则保持宽松。

背景：SnowLuma 群文件上传失败会返回 ``{"status":"ok","retcode":-134}``。
旧代码把这种响应当成功，于是「发出去了」是假的 —— 既没重试，也没回退成下载链接。
判据现在放在能力表的 ``strict_actions`` 里，由 ``OneBotAPI._call_api`` 统一执行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from AbstractionLayer.kernal import GENERIC, MUTATING_ACTIONS, NAPCAT, SNOWLUMA
from bot.services.forward import ForwardService
from transport.api import OneBotAPI, OneBotAPIError


class _Conn:
    """最小连接桩：只实现 OneBotAPI 会用到的那几个属性。"""

    backend = "snowluma"
    history_anchor = "message_id"

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None, timeout=None):
        self.calls.append((action, dict(params or {})))
        return self.result


def _api(result) -> tuple[OneBotAPI, _Conn]:
    conn = _Conn(result)
    return OneBotAPI(conn), conn


def _run(coro):
    return asyncio.run(coro)


FAILED_UPLOAD = {
    "status": "ok",
    "retcode": -134,
    "message": "文件上传失败（-134）",
    "data": None,
}

# ── 判据：哪些动作必须成功 ──────────────────────────────


def test_mutating_actions_are_the_ones_with_side_effects():
    assert {
        "send_group_msg",
        "send_private_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "forward_group_single_msg",
        "forward_friend_single_msg",
        "group_poke",
        "friend_poke",
        "delete_msg",
    } <= MUTATING_ACTIONS
    # 查询类不在里面：它们失败是正常业务（消息撤回了、查不到人）
    assert not any(a.startswith("get_") for a in MUTATING_ACTIONS)


def test_both_backends_share_the_default_strict_set():
    assert NAPCAT.strict_actions == SNOWLUMA.strict_actions == MUTATING_ACTIONS
    assert GENERIC.strict_actions == MUTATING_ACTIONS


# ── 发送：失败必须抛 ────────────────────────────────────


def test_send_group_message_raises_on_retcode():
    api, _ = _api(FAILED_UPLOAD)
    with pytest.raises(OneBotAPIError) as info:
        _run(api.send_group_message(1, [{"type": "text", "data": {"text": "hi"}}]))
    assert info.value.retcode == -134
    assert "send_group_msg" in str(info.value)


def test_forward_single_msg_raises_on_failure():
    """单条转发以前返回 True 就完事，失败是静默的 —— 现在必须抛。"""
    api, _ = _api({"status": "failed", "retcode": 100, "message": "消息不存在"})
    with pytest.raises(OneBotAPIError):
        _run(api.forward_group_single_msg(group_id=1, message_id=2))


def test_send_poke_raises_on_failure():
    api, _ = _api({"status": "failed", "retcode": 1400, "message": "对方不是好友"})
    with pytest.raises(OneBotAPIError):
        _run(api.send_poke(user_id=1, group_id=2))


def test_strict_check_covers_images_too():
    """图片走的是同一个 action（send_group_msg），不该漏掉。"""
    api, _ = _api({"status": "failed", "retcode": -134})
    with pytest.raises(OneBotAPIError):
        _run(api.send_group_img(1, "/tmp/a.jpg"))


# ── 查询：失败不该抛 ────────────────────────────────────


def test_get_actions_stay_lenient():
    payload = {"status": "failed", "retcode": 100, "message": "消息不存在"}
    api, _ = _api(payload)

    assert _run(api.get_message(123)) == payload
    assert _run(api.get_group_list()) == payload


def test_capabilities_can_opt_out(monkeypatch):
    """能力表把动作摘掉后就不再抛 —— 顺序/严格度都是数据，不是代码。"""
    api, _ = _api(FAILED_UPLOAD)
    assert api.strict_actions == MUTATING_ACTIONS
    api._strict_actions = frozenset()

    assert _run(api.send_group_message(1, []))["retcode"] == -134


def test_ensure_ok_still_rejects_unknown_status():
    """``status=async`` 这种既不是 ok 也不是 failed 的，按失败处理。"""
    api, _ = _api({"status": "async", "retcode": 0})
    with pytest.raises(OneBotAPIError):
        _run(api.send_group_message(1, []))


# ── 调用方：失败要落到「确定失败」，不能算成「不确定」 ─────────


class _Api:
    def __init__(self, exc):
        self.exc = exc

    async def send_group_forward_msg(self, **_kwargs):
        raise self.exc

    async def send_private_forward_msg(self, **_kwargs):
        raise self.exc


def _service(exc) -> ForwardService:
    service = ForwardService.__new__(ForwardService)

    async def _ensure_connected():
        return None

    service.mp = SimpleNamespace(ensure_connected=_ensure_connected)
    service.api = _Api(exc)
    return service


def test_send_rebuilt_reports_definite_failure():
    """retcode 被拒 = 确定没发出去 → False，调用方可以安全退回整卡转发。"""
    exc = OneBotAPIError("send_group_forward_msg", -134, "文件上传失败")
    assert _run(_service(exc)._send_rebuilt(group_id=1, nodes=[])) is False


def test_send_rebuilt_keeps_timeout_as_unknown():
    """超时仍然是「不确定」，语义不能被合并成 False。"""
    assert _run(_service(TimeoutError())._send_rebuilt(group_id=1, nodes=[])) is None
