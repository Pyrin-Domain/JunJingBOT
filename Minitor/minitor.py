import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import websockets
import json
import re
from collections.abc import Callable
from NapCatTools import MessageProcessor
from Websockets import NapCatBotConfig



async def listen_msg(config:NapCatBotConfig,process_message: Callable):
    async with websockets.connect(config.WS_URL) as websocket:
        print("已连接NapCat，开始监听消息...")
        while True:
            # 接收NapCat推送的事件
            data = await websocket.recv()
            event = json.loads(data)

            # 只处理消息事件
            if event.get("post_type") == "message":
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

if __name__ == "__main__":
    config = NapCatBotConfig()
    sender = NapCatBotConfig("sender_config.json")
    Messageprocessor = MessageProcessor(sender)
    asyncio.run(listen_msg(config, Messageprocessor.process_message))
