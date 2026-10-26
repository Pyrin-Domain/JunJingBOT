"""事件监听：把 OneBot 后端推来的消息交给 ``process_message``。

支持 NapCat / SnowLuma。与旧 ``Minitor/minitor.py`` 的 ``listen_msg`` 一致：

- 只处理 ``post_type == "message"`` 的事件，其它（心跳、通知、``message_sent``）
  直接丢弃；SnowLuma 会把 Bot 自己发出的消息标成 ``message_sent``，忽略它可以
  避免自问自答；
- 每条事件一个 ``asyncio.create_task``，并发执行，不阻塞 reader；
- 外部已经建好连接时直接复用（``conn=``），否则按配置自己建一个；
- 正常情况一直挂着，直到任务被取消，最后返回实际使用的连接。

连接层只懂协议，这里也不 import 任何业务模块。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

from .config import ConnectionSettings
from .connection import OneBotConnection


async def listen(
    config: ConnectionSettings | dict | str | None,
    process_message: Callable,
    *,
    conn: OneBotConnection | None = None,
    rws: OneBotConnection | None = None,
) -> OneBotConnection:
    """连接 OneBot 后端并监听消息事件。

    :param config: ``ConnectionSettings``（或 dict / 路径）；外部已建好连接时只用来打印
    :param process_message: 收到 ``post_type == "message"`` 时的回调（协程 / 普通函数都行）
    :param conn: 外部建好的连接（BotApp 共享同一个连接时传入）；不传就自己建一个
    :param rws: ``conn`` 的旧参数名（兼容老调用）
    :return: 实际使用的 :class:`OneBotConnection`
    """
    if conn is None:
        conn = rws
    if conn is None and isinstance(config, OneBotConnection):
        conn = config
    if conn is None:
        conn = OneBotConnection(config, role="事件")
        await conn.start()

    async def _event_handler(event: dict):
        if not isinstance(event, dict) or event.get("post_type") != "message":
            return
        task = asyncio.create_task(_call_process(event))

        def _done(t: asyncio.Task):
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                print(f"消息处理异常：{exc!r}")

        task.add_done_callback(_done)

    async def _call_process(event: dict):
        result = process_message(event)
        if inspect.isawaitable(result):
            await result

    conn.set_event_handler(_event_handler)

    # 等待后端连接
    await conn.wait_for_connect()
    label = getattr(getattr(conn, "config", None), "backend_label", "OneBot")
    print(f"{label} 已连接，开始监听消息（{conn.config.mode_label}）…")

    # 永远挂起（事件由连接层的 reader 驱动）
    await asyncio.Event().wait()
    return conn


# 旧名字：新代码用 listen()，老代码（或注释）里还能对上
listen_msg = listen


__all__ = ["listen", "listen_msg"]
