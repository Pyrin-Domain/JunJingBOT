"""君景 BOT 启动入口。

只做四件事：

    读配置 → 建连接 → 拉起 BotApp + 网页面板 → 监听事件

业务逻辑在 ``bot/`` 下；连接在 ``transport/``；面板在 ``web/`` + ``frontend/``。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from bot.app import BotApp
from config import load_settings
from core.logging import setup as setup_logging
from images import configure_from_settings
from transport import OneBotConnection, listen
from web import WebServer


async def build_app(settings):
    """按 config.json 的 type / backend 建连接与 BotApp。"""
    config = settings.connection

    if config.is_reverse:
        # 反向：后端主动连过来；同一条连接收事件 + 发 API
        events_conn = OneBotConnection(config, role="事件+API")
        await events_conn.start()
        api_conn = events_conn
    else:
        # 正向：事件连接 + sender_config.json 独立 API 连接
        events_conn = OneBotConnection(config, role="事件")
        await events_conn.start()
        api_conn = OneBotConnection(settings.api_connection, role="API")
        await api_conn.start()

    app = BotApp(
        config=config,
        connection=events_conn,
        api_connection=api_conn,
        settings=settings,
    )
    return app, events_conn, api_conn


async def main() -> None:
    # 固定按 run.py 所在目录找 config/，从任何 cwd 启动都不会读错文件。
    settings = load_settings(Path(__file__).resolve().parent)
    settings.paths.ensure_dirs()
    setup_logging(log_file=settings.paths.logs_dir / "bot.log")
    print(f"[启动] {settings.layout_report()}")
    print(
        f"[启动] bot_env={settings.env.bot_env.value} "
        f"backend_env={settings.env.backend_env.value} "
        f"profile={settings.env.path_profile} "
        f"imgs={settings.paths.images_dir}"
    )
    configure_from_settings(settings)

    app, events_conn, api_conn = await build_app(settings)
    print(
        f"[启动] 后端：{settings.connection.backend_label}"
        f"（{settings.connection.backend}）"
    )
    print(f"[启动] 连接方式：{settings.connection.mode_label}（{settings.connection.path}）")

    # 面板先起：后端连不上 / 连错了也能打开面板改配置
    web = WebServer(bot=app, settings=settings)
    await web.start_if_enabled()

    # 注册事件监听，再等连接；BotApp.start 里取 self_id、加载断点
    listener = asyncio.create_task(
        listen(settings.connection, app.process_message, conn=events_conn),
        name="onebot-listener",
    )
    await asyncio.sleep(0)

    try:
        await app.start()
        await listener
    finally:
        listener.cancel()
        await web.stop()
        await app.stop()
        await events_conn.stop()
        if api_conn is not events_conn:
            await api_conn.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[退出] 已停止")
