"""HTTP 路由注册。

``register_routes(app)`` 是唯一的入口；每个模块只负责自己的端点。
所有 handler 都从 ``request.app["web_server"]`` 拿到 :class:`web.server.WebServer`，
共享同一个 bot / settings 实例，不复制状态。
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

WEB_SERVER_KEY = "web_server"


def json_response(payload: Any, status: int = 200) -> web.Response:
    """统一的中文 JSON 响应（不转义，前端直接显示）。"""
    return web.json_response(
        payload,
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def server_of(request: web.Request):
    """取当前面板实例；没有就抛 500，避免 handler 里到处判空。"""
    server = request.app.get(WEB_SERVER_KEY)
    if server is None:  # pragma: no cover - 只在装配错误时发生
        raise web.HTTPInternalServerError(text="web server context missing")
    return server


async def read_body(request: web.Request) -> Any:
    """读 JSON body；非法 JSON / 空 body 返回 ``None``。"""
    try:
        return await request.json()
    except Exception:
        return None


def extract_data(body: Any) -> Any:
    """兼容 ``{data: {...}}`` 与直接提交对象两种写法。"""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def register_routes(app: web.Application) -> None:
    """把各模块的端点挂到 aiohttp app 上。"""
    # 延迟 import：子模块会反过来 import 本文件的 json_response / server_of，
    # 放在函数里可以避免包初始化期的循环引用。
    from . import config as config_routes
    from . import groups, paths, status, ws_settings

    status.register(app)
    config_routes.register(app)
    groups.register(app)
    ws_settings.register(app)
    paths.register(app)


__all__ = [
    "WEB_SERVER_KEY",
    "extract_data",
    "json_response",
    "read_body",
    "register_routes",
    "server_of",
]
