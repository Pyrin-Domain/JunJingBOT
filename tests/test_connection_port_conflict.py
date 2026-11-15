"""反向 WS 端口被占用时的报错：要说人话，别甩 winerror 10048。

背景：反向模式下 Bot 自己起 WS 服务器（默认 127.0.0.1:8085），
后端反连进来。端口被占 = 多半是上一个 Bot 实例还在跑 —— 这是最常见的
「双击两次 run.bat」事故，用户看到裸的 OSError 完全不知道该干嘛。
"""

import asyncio
import socket

import pytest

from transport.connection import OneBotConnection


def _free_port() -> int:
    """借一个当前没人用的端口号（借完就还，靠它自然回收）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(coro):
    return asyncio.run(coro)


def test_port_conflict_raises_readable_error():
    """端口被占 → 抛 OSError，消息里带端口号和「已有实例在跑」的提示。"""

    async def scenario():
        port = _free_port()
        # 先占住端口，模拟「另一个 Bot 实例已经起来了」
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            conn = OneBotConnection(
                {
                    "type": "ws_re",
                    "backend": "napcat",
                    "ws_reverse_host": "127.0.0.1",
                    "ws_reverse_port": port,
                }
            )
            with pytest.raises(OSError) as excinfo:
                await conn.start()
            return excinfo.value
        finally:
            blocker.close()

    exc = _run(scenario())
    assert exc.errno == 10048, "应该保留 10048，方便上层识别"
    msg = str(exc)
    assert str(exc.strerror) in msg
    # 消息里要有：端口号、怎么处理、以及「已有实例」这个根因
    assert "ws_reverse_port" in msg
    assert "taskkill" in msg


def test_start_is_retryable_after_port_conflict():
    """端口冲突后 _started 要复位，否则这个连接对象就废了。"""

    async def scenario():
        port = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            conn = OneBotConnection(
                {
                    "type": "ws_re",
                    "backend": "napcat",
                    "ws_reverse_host": "127.0.0.1",
                    "ws_reverse_port": port,
                }
            )
            with pytest.raises(OSError):
                await conn.start()
            return conn.is_started
        finally:
            blocker.close()

    assert _run(scenario()) is False, "失败后 _started 必须复位，否则无法重试"


def test_port_holder_hint_returns_empty_without_crashing():
    """诊断只是个 hint：查不到进程也不能把启动搞挂。"""
    from transport.connection import _port_holder_hint

    # 一个几乎不可能被占的高位端口
    assert isinstance(_port_holder_hint(65533), str)
