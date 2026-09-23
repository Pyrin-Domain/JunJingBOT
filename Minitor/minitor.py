"""minitor —— 事件监听入口：收 NapCat 推来的消息，交给 process_message。

正向 / 反向由 config.json 的 ``type`` 决定（实现都在 Websockets.py）。
这里只做四件事：建连接（或复用调用方给的连接）→ 注册事件回调 →
等 NapCat 连上 → 挂起不返回。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import inspect
from collections.abc import Callable

try:  # 兼容 run.py 的扁平 import 和 `Minitor.minitor` 包内 import
    from Websockets import NapCatBotConfig, NapCatConnection
except ImportError:  # pragma: no cover
    from Minitor.Websockets import NapCatBotConfig, NapCatConnection


async def listen_msg(config: NapCatBotConfig, process_message: Callable,
                     conn: NapCatConnection | None = None,
                     rws: NapCatConnection | None = None) -> NapCatConnection:
    """连接 NapCat 并监听消息事件（正常情况一直挂着，直到被取消）。

    :param config: NapCatBotConfig；外部已经建好连接时只用来打印模式
    :param process_message: 收到 ``post_type == "message"`` 时调用的回调
                            （协程或普通函数都行，并发执行，不阻塞监听）
    :param conn: 外部建好的连接（run.py 里共享同一个连接时传入）；
                 不传就按 config 的模式自己建一个
    :param rws: ``conn`` 的旧参数名（兼容老调用）
    :return: 实际使用的 NapCatConnection
    """
    conn = conn if conn is not None else rws
    if conn is None:
        conn = NapCatConnection(config, role="事件")
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

    # 等待 NapCat 连接
    await conn.wait_for_connect()
    print(f"NapCat 已连接，开始监听消息（{conn.config.mode_label}）…")

    # 永远挂起（事件由连接层的 reader 驱动）
    await asyncio.Event().wait()
    return conn
