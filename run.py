"""君景 Bot 启动入口。

只负责「读配置 → 建连接 → 拉起 BOTMain / 网页面板」，业务逻辑都在 Minitor/ 下：

    Minitor/Websockets.py   正向(ws) / 反向(ws_re) 统一的 WS 连接层
    Minitor/BOTMain.py      主流程（事件路由 + 调用各服务）
    Minitor/BotConfig.py    可调参数 ←→ config/bot_config.json
    Minitor/WebPanel.py     网页参数面板（改参数不用改代码）

连接方式由 config/config.json 里的 type 决定：

    "type": "ws_re"  反向：NapCat 连到 Bot（ws_reverse_port），一条连接收事件 + 发 API
    "type": "ws"     正向：Bot 连到 NapCat（ws_host/ws_port）收事件，
                     发送 API 用 config/sender_config.json 那条连接
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Minitor"))
import asyncio

from BOTMain import BOTMain
from Paths import Paths
from Websockets import NapCatBotConfig, NapCatConnection
from minitor import listen_msg
from WebPanel import WebPanel


async def build_bot() -> tuple[NapCatBotConfig, NapCatConnection, BOTMain]:
    """按 config.json 的 type 建连接与 BOTMain，返回 (config, 事件连接, bot)。"""
    config = NapCatBotConfig()                      # config/config.json

    if config.is_reverse:
        # 反向：NapCat 主动连过来，同一条连接既收事件又发 API
        events_conn = NapCatConnection(config, role="事件+API")
        await events_conn.start()
        bot = BOTMain(config, conn=events_conn)
        return config, events_conn, bot

    # 正向：事件连接连 config.json，发送 API 走 sender_config.json（两条独立连接）
    sender = NapCatBotConfig("sender_config.json", required=True)
    events_conn = NapCatConnection(config, role="事件")
    await events_conn.start()
    api_conn = NapCatConnection(sender, role="API")
    await api_conn.start()
    bot = BOTMain(sender, conn=api_conn)
    return config, events_conn, bot


async def main():
    Paths.ensure_dirs()
    print(f"[启动] {Paths.layout_report()}")

    config, events_conn, bot = await build_bot()
    print(f"[启动] 连接方式：{config.mode_label}（{config.path}）")

    # ── 网页参数面板：改配置不用改代码（可在 bot_config.json 里关掉） ──
    # 放在等 NapCat 连接之前：网连不上 / 连错了也能打开面板改配置
    panel = WebPanel(bot, connection=events_conn)
    await panel.start_if_enabled()

    # 复用同一个连接监听事件（正向模式下事件连接与 API 连接相互独立）
    # 单独开成任务，让事件回调先注册上，不会被下面的等连接堵住
    listener = asyncio.create_task(
        listen_msg(config, bot.process_message, conn=events_conn)
    )
    await asyncio.sleep(0)

    await bot.setuserid()
    # 从断点恢复（不阻塞，后台回放）
    await bot.recover_from_check_point()

    await listener


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[退出] 已停止")
