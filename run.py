import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Minitor"))
import asyncio
from NapCatTools import MessageProcessor
from Websockets import NapCatBotConfig
from minitor import listen_msg


async def main():
    config = NapCatBotConfig()
    sender = NapCatBotConfig("sender_config.json")
    mp = MessageProcessor(sender)
    await mp.setuserid()
    # 从断点恢复（不阻塞，后台回放）
    await mp.recover_from_check_point()
    await listen_msg(config, mp.process_message)

if __name__ == "__main__":
    asyncio.run(main())