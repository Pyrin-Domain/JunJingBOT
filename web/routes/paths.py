"""路径映射端点（``config/environment.json``）。

- ``GET  /api/paths``          当前环境 / 翻译模式 / 规则 / 解析后的目录
- ``POST /api/paths``          校验 → 只合并路径字段并写盘 → 热替换翻译器
- ``POST /api/paths/preview``  按当前草稿预览一条路径的双向翻译结果

路径翻译不重建连接，所以保存后返回 ``restart_required: false``。
"""

from __future__ import annotations

from aiohttp import web

from config.environment import EnvConfig, detect_env, env_options
from config.path_profiles import path_profile_options
from core.jsonstore import read_json, write_json

from ..validation import validate_paths_config
from . import extract_data, json_response, read_body, server_of


def _env_path(server):
    return server.settings.paths.config_file("environment.json")


def _resolved(settings) -> dict[str, str]:
    return {
        "root": str(settings.paths.root),
        "imgs_dir": str(settings.paths.images_dir),
        "downloads_dir": str(settings.paths.downloads_dir),
        "temp_dir": str(settings.paths.temp_dir),
    }


def _payload(server) -> dict:
    settings = server.settings
    env_path = _env_path(server)
    return {
        "ok": True,
        "data": settings.env.to_dict(),
        "profiles": path_profile_options(),
        "env_options": env_options(),
        "detected": {"bot_env": detect_env().value},
        "resolved": _resolved(settings),
        "dirs": settings.env_dirs,
        "meta": {"env_path": str(env_path), "exists": env_path.exists()},
    }


async def handle_get_paths(request: web.Request) -> web.StreamResponse:
    return json_response(_payload(server_of(request)))


async def handle_save_paths(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    settings = server.settings

    body = await read_body(request)
    data = extract_data(body)
    if not isinstance(data, dict):
        return json_response(
            {"ok": False, "errors": ["请求体必须是 JSON 对象或 {data: {...}}"]},
            status=400,
        )

    errors, warnings, cleaned = validate_paths_config(data)
    if errors:
        return json_response(
            {"ok": False, "errors": errors, "warnings": warnings}, status=400
        )

    env_path = _env_path(server)
    try:
        merged = read_json(env_path, {})
        if not isinstance(merged, dict):
            merged = {}
        # 只清理这次真正写了的字段的旧别名，其它目录 / 环境字段原样保留
        if "backend_env" in cleaned:
            merged.pop("onebot_env", None)
            merged.pop("napcat_env", None)
            merged.pop("napcat_environment", None)
        if "path_profile" in cleaned:
            merged.pop("profile", None)
            merged.pop("path_map_profile", None)
        merged.update(cleaned)
        write_json(env_path, merged)
    except OSError as exc:
        return json_response(
            {"ok": False, "errors": [f"写入失败：{exc}"]}, status=500
        )

    reload_error: str | None = None
    try:
        settings.reload()
    except Exception as exc:  # pragma: no cover - 配置损坏时保持旧内存值
        reload_error = str(exc)
        print(f"[Web] 重新读取路径配置失败：{exc}")

    payload = _payload(server)
    payload.update(
        {
            "warnings": warnings,
            "saved_to": str(env_path),
            "restart_required": False,
            "applied": ["路径映射（热加载）"],
        }
    )
    if reload_error:
        payload["reload_error"] = reload_error
    return json_response(payload)


async def handle_preview_paths(request: web.Request) -> web.StreamResponse:
    server = server_of(request)
    settings = server.settings

    body = await read_body(request)
    if not isinstance(body, dict):
        return json_response(
            {"ok": False, "errors": ["请求体必须是 JSON 对象"]}, status=400
        )

    value = str(body.get("value") or "").strip()
    if not value:
        return json_response(
            {"ok": False, "errors": ["请填写要预览的路径"]}, status=400
        )

    draft = body.get("data")
    if not isinstance(draft, dict):
        draft = {}

    mapping = dict(settings.env.to_dict())
    for key in (
        "bot_env",
        "backend_env",
        "onebot_env",
        "napcat_env",
        "path_profile",
        "wsl_distro",
        "wsl_mount_root",
        "path_map",
    ):
        if key in draft:
            mapping[key] = draft[key]

    # 预览不读环境变量：面板上看到什么就按什么算。
    env = EnvConfig(
        bot_env=mapping.get("bot_env"),
        backend_env=(
            mapping.get("backend_env")
            or mapping.get("onebot_env")
            or mapping.get("napcat_env")
        ),
        path_profile=mapping.get("path_profile", ""),
        wsl_distro=mapping.get("wsl_distro", "Ubuntu"),
        wsl_mount_root=mapping.get("wsl_mount_root", "/mnt"),
        path_map=mapping.get("path_map") or (),
    )
    translator = env.translator
    backend_result = translator.explain(value, direction="backend")
    # 反向预览的是「真正发给协议端的那串路径」回来会变成什么，
    # 而不是拿用户输入直接反查（用户可能输入的本来就是协议端路径）。
    local_result = translator.explain(backend_result["output"], direction="local")

    # 只取提示，不拦预览：用户可能正填到一半。
    _, warnings, _ = validate_paths_config(draft) if draft else ([], [], {})

    return json_response(
        {
            "ok": True,
            "input": value,
            "warnings": warnings,
            "env": {
                "bot_env": env.bot_env.value,
                "backend_env": env.backend_env.value,
                "path_profile": env.path_profile,
            },
            "to_backend": backend_result,
            "to_local": local_result,
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/paths", handle_get_paths)
    app.router.add_post("/api/paths", handle_save_paths)
    app.router.add_post("/api/paths/preview", handle_preview_paths)


__all__ = [
    "handle_get_paths",
    "handle_preview_paths",
    "handle_save_paths",
    "register",
]
