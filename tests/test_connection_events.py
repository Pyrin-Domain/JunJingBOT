"""连接层状态回调：每次连上 / 断开都要通知上层。"""

import asyncio

from transport.connection import OneBotConnection


class _OneFrameWS:
    """只产生一个“读完”信号的最小 WS，让连接回调有机会先执行。"""

    def __init__(self):
        self.request = None
        self.remote_address = ("127.0.0.1", 12345)
        self._yielded = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        await asyncio.sleep(0)
        raise StopAsyncIteration

    async def close(self):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_connect_listener_fires_for_reverse_client():
    async def scenario():
        conn = OneBotConnection({"type": "ws_re", "backend": "napcat"})
        seen = []

        async def on_connect():
            seen.append(("connect", conn.is_connected))

        conn.add_connect_listener(on_connect)
        await conn._on_client(_OneFrameWS())
        await asyncio.sleep(0)
        return seen

    assert _run(scenario()) == [("connect", True)]


def test_connect_and_disconnect_listeners_fire_in_order():
    async def scenario():
        conn = OneBotConnection({"type": "ws_re", "backend": "napcat"})
        seen = []

        conn.add_connect_listener(lambda: seen.append("connect"))
        conn.add_disconnect_listener(lambda: seen.append("disconnect"))
        await conn._on_client(_OneFrameWS())
        await asyncio.sleep(0)
        return seen

    assert _run(scenario()) == ["connect", "disconnect"]
