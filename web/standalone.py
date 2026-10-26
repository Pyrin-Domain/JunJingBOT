"""只启动网页面板（不连 NapCat，不跑 Bot）。

    python -m web.standalone
    python -m web.standalone --host 0.0.0.0 --port 8390 --token 123456
    python -m web.standalone --config config/bot_config.json

独立模式下：

- 读 / 写 ``config/bot_config.json`` 与 ``config/config.json`` 正常；
- 运行状态只能显示静态信息（Bot 不在场）；
- 保存后返回 ``restart_required``，下次 ``python run.py`` 生效。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import load_settings
from core.jsonstore import read_json

from .server import WebServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="君景 Bot 参数面板（可脱离 Bot 单独运行）"
    )
    parser.add_argument("--root", default=None, help="项目根目录，默认自动探测")
    parser.add_argument("--config", default=None, help="bot_config.json 路径")
    parser.add_argument("--host", default=None, help="监听地址，默认取配置文件")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取配置文件")
    parser.add_argument("--token", default=None, help="访问 token，默认取配置文件")
    parser.add_argument("--static-dir", default=None, help="静态页面目录（frontend/）")
    return parser


def create_server(args: argparse.Namespace) -> WebServer:
    settings = load_settings(args.root)

    if args.config:
        config_path = Path(args.config)
        data = read_json(config_path, {})
        if not isinstance(data, dict):
            raise SystemExit(f"配置文件不是 JSON 对象：{config_path}")
        settings.update(data)
        settings.path = config_path

    return WebServer(
        bot=None,
        settings=settings,
        host=args.host,
        port=args.port,
        token=args.token,
        static_dir=args.static_dir,
    )


async def _serve(panel: WebServer) -> None:
    print(f"[Web] 参数面板（无 Bot 模式）：{panel.url}")
    print(f"[Web] 配置文件：{panel.settings.path}")
    print("[Web] Ctrl+C 退出")
    await panel.start()
    try:
        await asyncio.Event().wait()
    finally:
        await panel.stop()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    panel = create_server(args)
    try:
        asyncio.run(_serve(panel))
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        print("\n[Web] 已退出")


if __name__ == "__main__":
    sys.exit(main())
