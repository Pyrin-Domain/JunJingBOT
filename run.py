import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Minitor"))
import asyncio
from NapCatTools import MessageProcessor
from Websockets import NapCatBotConfig
from minitor import listen_msg
from reverse_ws import NapCatReverseWS


async def main():
    # 读取配置
    config = NapCatBotConfig()
    sender = NapCatBotConfig("sender_config.json")

    # ── 创建共享反向 WebSocket 连接（NapCat 主动连 Bot） ──
    rws = NapCatReverseWS(
        host=config.ws_reverse_host,
        port=config.ws_reverse_port,
        token=config.token,
    )
    await rws.start()

    # MessageProcessor 使用反向 WS 进行 API 调用
    mp = MessageProcessor(sender, rws=rws)
    await mp.setuserid()
    # 从断点恢复（不阻塞，后台回放）
    await mp.recover_from_check_point()
    # listen_msg 复用同一个 rws 监听事件
    await listen_msg(config, mp.process_message, rws=rws)

if __name__ == "__main__":
    asyncio.run(main())