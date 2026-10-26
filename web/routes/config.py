"""配置读写端点。

- ``GET  /api/config``    当前生效配置 + action 名 + 图片目录 + 默认值 / schema
- ``POST /api/config``    校验 → 原子写盘 → 热加载
- ``POST /api/validate``  只校验，不保存
- ``POST /api/reload``    重新读盘 + 热加载

保存流程严格保持旧面板的语义：
``validate`` → ``settings.update`` → ``settings.save`` → ``bot.reload_config``。
``web_panel.*`` 与连接方式只影响启动参数，返回 ``restart_required``。
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from config.defaults import default_bot_config

from ..validation import validate_config
from . import extract_data, json_response, read_body, server_of

# 热加载后立即生效的参数段（与旧 BOTMain.reload_config 的 applied 一致）
APPLIED_SECTIONS: tuple[str, ...] = (
    "keyword_actions",
    "auto_forward",
    "image_forward",
    "jm",
    "prompts",
    "replies",
    "master_qq",
    "heartbeat",
    "message.extraconfig",
    "agent",
    "interject",
)


def build_schema() -> dict:
    """给前端 / 调试用的轻量 schema：只描述段名和默认值类型。

    真正的校验逻辑在 :mod:`web.validation`；这里不重复定义规则，
    只让调用方知道有哪些段、每段有哪些键。
    """
    defaults = default_bot_config()
    sections: dict[str, Any] = {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            sections[key] = sorted(value.keys())
        elif isinstance(value, list):
            sections[key] = {"type": "array", "default": value}
        else:
            sections[key] = {"type": type(value).__name__, "default": value}
    return {"version": 1, "sections": sections}


def _panel_changed(old_panel: dict, new_panel: dict) -> bool:
    return old_panel != new_panel


def _restart_reason(server, old_panel: dict, old_conn_type: str) -> tuple[bool, str]:
    """对比保存前后的启动参数，返回 (是否需要重启, 原因)。"""
    reasons: list[str] = []
    if _panel_changed(old_panel, dict(server.settings.section("web_panel"))):
        reasons.append("web_panel.* 的改动要重启 Bot 才生效")
    if old_conn_type and old_conn_type != server.settings.connection.type:
        reasons.append("连接方式（config.json 的 type）改动要重启 Bot 才生效")
    return bool(reasons), "；".join(reasons)


async def handle_get_config(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    settings = server.settings
    return json_response(
        {
            "ok": True,
            "data": settings.to_dict(),
            "meta": server.config_meta(),
            "actions": server.action_names(),
            "imgs_dir": server.imgs_dir,
            "defaults": default_bot_config(),
            "schema": build_schema(),
        }
    )


async def handle_validate(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    body = await read_body(request)
    data = extract_data(body)
    if data is None:
        return json_response(
            {"ok": False, "errors": ["请求体不是合法 JSON"]}, status=400
        )
    errors, warnings = validate_config(
        data,
        imgs_dir=server.imgs_dir,
        known_actions=server.action_names(),
    )
    return json_response({"ok": not errors, "errors": errors, "warnings": warnings})


async def handle_save_config(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    settings = server.settings

    body = await read_body(request)
    data = extract_data(body)
    if not isinstance(data, dict):
        return json_response(
            {
                "ok": False,
                "errors": ["请求体必须是 JSON 对象或 {data: {...}}"],
            },
            status=400,
        )

    errors, warnings = validate_config(
        data,
        imgs_dir=server.imgs_dir,
        known_actions=server.action_names(),
    )
    if errors:
        return json_response(
            {"ok": False, "errors": errors, "warnings": warnings}, status=400
        )

    old_panel = dict(settings.section("web_panel"))
    old_conn_type = settings.connection.type

    try:
        settings.update(data)
        saved_to = settings.save()
    except Exception as exc:  # 磁盘权限 / JSON 序列化失败
        return json_response(
            {"ok": False, "errors": [f"写入失败：{exc}"]}, status=500
        )

    result: dict[str, Any]
    if server.bot is not None:
        try:
            result = await server.reload_bot()
        except Exception as exc:  # 热加载失败不应该让保存整体失败
            result = {
                "applied": [],
                "reload_error": str(exc),
                "restart_required": False,
            }
    else:
        # 独立模式：写盘 + 重新读盘，Bot 进程不在，下次启动才生效
        settings.reload()
        result = {
            "applied": ["（未连接 Bot，参数已写入文件）"],
            "restart_required": True,
            "restart_reason": "面板在独立模式下运行，改动会在 Bot 下次启动时生效",
        }

    restart_required, reason = _restart_reason(server, old_panel, old_conn_type)
    if restart_required:
        result["restart_required"] = True
        previous = str(result.get("restart_reason") or "").strip()
        result["restart_reason"] = "；".join(part for part in (previous, reason) if part)

    result.setdefault("path", str(settings.path))
    result.setdefault("saved_to", saved_to)
    result.setdefault("applied", list(APPLIED_SECTIONS))

    return json_response(
        {
            "ok": True,
            "warnings": warnings,
            "result": result,
            "data": settings.to_dict(),
            "meta": server.config_meta(),
        }
    )


async def handle_reload(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    settings = server.settings

    reload_error: str | None = None
    try:
        settings.reload()
    except Exception as exc:  # pragma: no cover - 读盘异常
        reload_error = str(exc)

    if server.bot is not None:
        try:
            result = await server.reload_bot()
        except Exception as exc:
            result = {"applied": [], "reload_error": str(exc)}
    else:
        result = {
            "applied": ["（未连接 Bot）"],
            "restart_required": True,
            "restart_reason": "面板没有连接 Bot，只能重新读盘",
        }

    if reload_error and "reload_error" not in result:
        result["reload_error"] = reload_error

    return json_response(
        {
            "ok": True,
            "result": result,
            "data": settings.to_dict(),
            "meta": server.config_meta(),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_post("/api/config", handle_save_config)
    app.router.add_post("/api/validate", handle_validate)
    app.router.add_post("/api/reload", handle_reload)


__all__ = [
    "APPLIED_SECTIONS",
    "build_schema",
    "handle_get_config",
    "handle_reload",
    "handle_save_config",
    "handle_validate",
    "register",
]
