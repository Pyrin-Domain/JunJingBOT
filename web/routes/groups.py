"""``GET /api/groups`` —— 群列表（面板下拉框用）。

旧实现在正向模式下可能拿发送连接的 API 去要群列表（甚至直接拿不到）。
这里明确只用**事件连接**：``bot.events_connection`` → ``bot.conn``。
"""

from __future__ import annotations

import inspect
import time

from aiohttp import web

from . import json_response, server_of

GROUP_CACHE_TTL = 60.0


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


async def _call_group_list(conn) -> dict | list:
    """按鸭子类型调用 ``get_group_list``，兼容三种连接封装。"""
    call_api = getattr(conn, "call_api", None)
    if callable(call_api):
        return await _maybe_await(call_api("get_group_list", {}))

    direct = getattr(conn, "get_group_list", None)
    if callable(direct):
        return await _maybe_await(direct())

    api = getattr(conn, "api", None)
    if api is not None:
        api_call = getattr(api, "get_group_list", None)
        if callable(api_call):
            return await _maybe_await(api_call())

    raise RuntimeError("连接对象没有 call_api / get_group_list，取不到群列表")


def _parse_groups(response) -> list[dict]:
    raw = response
    if isinstance(response, dict):
        raw = response.get("data")
        if raw is None:
            raw = response.get("groups")
    groups: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        group_id = item.get("group_id")
        if group_id is None:
            group_id = item.get("group")
        if group_id is None:
            continue
        groups.append(
            {
                "group_id": group_id,
                "group_name": str(item.get("group_name") or group_id),
            }
        )
    return groups


async def handle_groups(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    now = time.monotonic()

    if server._group_cache and now - server._group_cache_at < GROUP_CACHE_TTL:
        return json_response(
            {"ok": True, "groups": server._group_cache, "cached": True}
        )

    conn = server.events_connection
    if conn is None:
        return json_response(
            {
                "ok": True,
                "groups": [],
                "error": "面板没跟 Bot 同一个进程，取不到群列表（手动填群号也行）",
            }
        )

    if getattr(conn, "is_connected", None) is False:
        return json_response(
            {"ok": True, "groups": [], "error": "事件连接还没建立，稍后再试（手动填群号也行）"}
        )

    try:
        response = await _call_group_list(conn)
        groups = _parse_groups(response)
    except Exception as exc:
        return json_response(
            {"ok": True, "groups": [], "error": f"取群列表失败：{exc}"}
        )

    server._group_cache = groups
    server._group_cache_at = now
    return json_response({"ok": True, "groups": groups, "cached": False})


def register(app: web.Application) -> None:
    app.router.add_get("/api/groups", handle_groups)


__all__ = ["GROUP_CACHE_TTL", "handle_groups", "register"]
