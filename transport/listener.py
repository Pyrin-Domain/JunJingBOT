"""事件监听：把 OneBot 后端推来的消息交给 ``process_message``。

支持 NapCat / SnowLuma。与旧 ``Minitor/minitor.py`` 的 ``listen_msg`` 一致：

- 只处理 ``post_type == "message"`` 的事件，其它（心跳、通知、``message_sent``）
  直接丢弃；SnowLuma 会把 Bot 自己发出的消息标成 ``message_sent``，忽略它可以
  避免自问自答；
- 同一群 / 私聊的消息按到达顺序串行，不同会话并行，并限制总并发与积压；
- 外部已经建好连接时直接复用（``conn=``），否则按配置自己建一个；
- 正常情况一直挂着，直到任务被取消，最后返回实际使用的连接。

连接层只懂协议，这里也不 import 任何业务模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from core.logging import get_logger

from .config import ConnectionSettings
from .connection import OneBotConnection
from .event_dispatcher import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_PENDING,
    EventDispatcher,
)

log = get_logger("transport.listener")


async def listen(
    config: ConnectionSettings | dict | str | None,
    process_message: Callable,
    *,
    conn: OneBotConnection | None = None,
    rws: OneBotConnection | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_pending: int = DEFAULT_MAX_PENDING,
) -> OneBotConnection:
    """连接 OneBot 后端并监听消息事件。

    :param config: ``ConnectionSettings``（或 dict / 路径）；外部已建好连接时只用来打印
    :param process_message: 收到 ``post_type == "message"`` 时的回调（协程 / 普通函数都行）
    :param conn: 外部建好的连接（BotApp 共享同一个连接时传入）；不传就自己建一个
    :param rws: ``conn`` 的旧参数名（兼容老调用）
    :param max_concurrency: 同时处理的会话数上限
    :param max_pending: 尚未处理完的事件总数上限
    :return: 实际使用的 :class:`OneBotConnection`
    """
    if conn is None:
        conn = rws
    if conn is None and isinstance(config, OneBotConnection):
        conn = config
    if conn is None:
        conn = OneBotConnection(config, role="事件")
        await conn.start()

    dispatcher = EventDispatcher(
        process_message,
        max_concurrency=max_concurrency,
        max_pending=max_pending,
    )

    def _event_handler(event: dict):
        if not isinstance(event, dict) or event.get("post_type") != "message":
            return
        dispatcher.submit(event)

    conn.set_event_handler(_event_handler)

    try:
        # 等待后端连接
        await conn.wait_for_connect()
        label = getattr(getattr(conn, "config", None), "backend_label", "OneBot")
        log.info(
            "%s 已连接，开始监听消息（%s）…",
            label,
            conn.config.mode_label,
        )

        # 永远挂起（事件由连接层的 reader 驱动）
        await asyncio.Event().wait()
    finally:
        await dispatcher.close()
    return conn


# 旧名字：新代码用 listen()，老代码（或注释）里还能对上
listen_msg = listen


__all__ = ["listen", "listen_msg"]
