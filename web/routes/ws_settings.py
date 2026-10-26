"""OneBot 连接设置端点（``config/config.json`` / ``sender_config.json``）。

``GET  /api/ws``  读取连接配置 + 当前连接状态
``POST /api/ws``  只合并连接相关字段并原子写盘；其余字段原样保留

连接在启动时建立，所以保存后永远返回 ``restart_required: true``。
"""

from __future__ import annotations

from aiohttp import web

from config.backends import DEFAULT_BACKEND, backend_label, backend_options
from core.jsonstore import read_json, write_json

from ..validation import validate_ws_config, ws_section
from . import extract_data, json_response, read_body, server_of


def _config_path(server, name: str):
    """现在正在使用的那份配置路径（config/ 优先，兼容根目录旧文件）。"""
    return server.settings.paths.config_file(name)


async def handle_get_ws(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    config_path = _config_path(server, "config.json")
    sender_path = _config_path(server, "sender_config.json")
    data = ws_section(read_json(config_path, {}))
    sender = ws_section(read_json(sender_path, {}))
    # 发送连接默认继承事件连接的后端；面板上显示继承后的值。
    if not sender.get("backend"):
        sender["backend"] = data.get("backend") or DEFAULT_BACKEND
    return json_response(
        {
            "ok": True,
            "data": data,
            "sender": sender,
            "backends": backend_options(),
            "default_backend": DEFAULT_BACKEND,
            "meta": {
                "config_path": str(config_path),
                "sender_path": str(sender_path),
                "config_exists": config_path.exists(),
                "sender_exists": sender_path.exists(),
            },
            "info": server.connection_info(),
        }
    )


async def handle_save_ws(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    body = await read_body(request)
    data = extract_data(body)
    if not isinstance(data, dict):
        return json_response(
            {"ok": False, "errors": ["请求体必须是 JSON 对象或 {data: {...}}"]},
            status=400,
        )

    sender_data = body.get("sender") if isinstance(body, dict) else None

    errors, cleaned = validate_ws_config(data)
    sender_cleaned: dict = {}
    if isinstance(sender_data, dict) and sender_data:
        sender_errors, sender_cleaned = validate_ws_config(sender_data)
        errors = errors + sender_errors
        # 发送连接与事件连接使用同一个后端；面板没单独选时跟随主配置。
        if not sender_cleaned.get("backend") and cleaned.get("backend"):
            sender_cleaned["backend"] = cleaned["backend"]

    if errors:
        return json_response({"ok": False, "errors": errors}, status=400)

    written: list[str] = []
    try:
        # 只改连接相关字段，文件里其它内容（如 forward_dedup）原样保留
        config_path = _config_path(server, "config.json")
        merged = read_json(config_path, {})
        if not isinstance(merged, dict):
            merged = {}
        merged.update(cleaned)
        write_json(config_path, merged)
        written.append(str(config_path))

        if sender_cleaned:
            sender_path = _config_path(server, "sender_config.json")
            sender_merged = read_json(sender_path, {})
            if not isinstance(sender_merged, dict):
                sender_merged = {}
            sender_merged.update(sender_cleaned)
            write_json(sender_path, sender_merged)
            written.append(str(sender_path))
    except OSError as exc:
        return json_response(
            {"ok": False, "errors": [f"写入失败：{exc}"]}, status=500
        )

    # 重新读盘，让 connection_info() 反映刚写入的配置（连接本身仍需重启）
    try:
        server.settings.reload()
    except Exception as exc:  # pragma: no cover - 配置损坏时保持旧内存值
        print(f"[Web] 重新读取连接配置失败：{exc}")

    config_path = _config_path(server, "config.json")
    label = backend_label(getattr(server.settings.connection, "backend", None))
    return json_response(
        {
            "ok": True,
            "saved_to": written,
            "restart_required": True,
            "hint": f"连接方式改动要重启 Bot 才生效（{label} 那边也要填一样的端口）",
            "data": ws_section(read_json(config_path, {})),
            "info": server.connection_info(),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/ws", handle_get_ws)
    app.router.add_post("/api/ws", handle_save_ws)


__all__ = ["handle_get_ws", "handle_save_ws", "register"]
