"""``GET /api/status`` —— 运行状态。

返回字段与旧面板保持一致，另外补了 ``checkpoint_count`` / ``group_count`` /
``ws`` 摘要。**绝不返回面板 token 明文**（旧版 bug，已修）。
"""

from __future__ import annotations

from aiohttp import web

from . import json_response, server_of


async def handle_status(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    return json_response(server.status_payload())


def register(app: web.Application) -> None:
    app.router.add_get("/api/status", handle_status)


__all__ = ["handle_status", "register"]
