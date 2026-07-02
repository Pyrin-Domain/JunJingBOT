import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import json
import re
from collections.abc import Callable
from Websockets import NapCatBotConfig
from reverse_ws import NapCatReverseWS


async def listen_msg(config:NapCatBotConfig, process_message: Callable,
                     rws: NapCatReverseWS | None = None) -> NapCatReverseWS:
    """启动反向 WebSocket 服务器，NapCat 连接后开始监听事件。

    如果外部已经创建了 ``NapCatReverseWS`` 实例（用于共享 API 连接），
    可通过 ``rws`` 传入；否则内部自动创建。
    返回 ``NapCatReverseWS`` 实例引用。
    """
    if rws is None:
        rws = NapCatReverseWS(
            host=config.ws_reverse_host,
            port=config.ws_reverse_port,
            token=config.token,
        )
        await rws.start()

    # 注册事件处理器
    async def _event_handler(event: dict):
        if event.get("post_type") != "message":
            return
        message_id = event["message_id"]
        user_id = event["user_id"]
        raw_msg = event["raw_message"]

        if event["message_type"] == "group":
            group_id = event["group_id"]
            print(f"【群{group_id}】用户{user_id} | msgID:{message_id} | 内容：{raw_msg}")
        else:
            print(f"【私聊】用户{user_id} | msgID:{message_id} | 内容：{raw_msg}")
        # 并发处理消息，不阻塞监听；异常会打印而不是静默吞掉
        task = asyncio.create_task(process_message(event))
        task.add_done_callback(
            lambda t: print(f"消息处理异常: {t.exception()}") if t.exception() else None
        )

    rws.set_event_handler(_event_handler)

    # 等待 NapCat 连接
    await rws.wait_for_connect()
    print("NapCat 已连接，开始监听消息（反向 WebSocket）...")

    # 永远挂起（由 reverse_ws 的 reader 驱动）
    await asyncio.Event().wait()
    return rws

