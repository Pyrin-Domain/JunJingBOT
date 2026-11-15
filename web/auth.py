"""面板 token 鉴权中间件。

只保护 ``/api/*``；``GET /`` 与静态资源公开，否则浏览器连页面都打不开。

token 接受三种来源（与旧面板兼容）：

1. ``Authorization: Bearer <token>``
2. ``X-Panel-Token: <token>``
3. ``?token=<token>``（方便直接贴链接）

比较用 :func:`hmac.compare_digest`，避免按字节泄露长度/前缀。
"""

from __future__ import annotations

import hmac
import ipaddress
from urllib.parse import urlsplit

from aiohttp import web

PROTECTED_PREFIX = "/api"


def token_matches(supplied: str | None, expected: str | None) -> bool:
    """常量时间比较（空 token 永远不匹配）。"""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(
        str(supplied).encode("utf-8"), str(expected).encode("utf-8")
    )


def extract_token(request: web.Request) -> str | None:
    """从请求里取 token；``Authorization`` 优先于 query。"""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        value = header[7:].strip()
        if value:
            return value
    legacy = request.headers.get("X-Panel-Token")
    if legacy:
        return legacy
    query = request.query.get("token")
    return query or None


def is_loopback_host(host: str | None) -> bool:
    """判断监听地址是否只对本机开放。"""
    value = str(host or "").strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def normalize_cors_origins(value) -> tuple[str, ...]:
    """规范化面板 CORS 白名单；``*`` 可显式放开所有来源。"""
    if value in (None, "", ()):
        return ()
    entries = [value] if isinstance(value, str) else list(value)
    normalized: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("每一项都必须是字符串")
        origin = entry.strip().rstrip("/")
        if origin == "*":
            normalized.append(origin)
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(f"{entry!r} 不是合法的来源（示例：https://panel.example.com）")
        normalized.append(f"{parsed.scheme.lower()}://{parsed.netloc}")
    return tuple(dict.fromkeys(normalized))


def make_auth_middleware(token: str | None):
    """构造 aiohttp 中间件；``token`` 为空时放行所有请求。"""

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        # 预检请求不带业务 token，交给 CORS 中间件应答
        if request.method == "OPTIONS":
            return await handler(request)
        if token and request.path.startswith(PROTECTED_PREFIX):
            if not token_matches(extract_token(request), token):
                return web.json_response(
                    {"ok": False, "errors": ["token 不正确"]},
                    status=401,
                    dumps=_dumps,
                )
        return await handler(request)

    return auth_middleware


def make_cors_middleware(*, allow_origins=()):
    """按白名单允许 ``frontend/`` 独立部署时跨源访问 ``/api``。

    同源请求始终放行；只给 ``/api/*`` 加 CORS 头，静态页面不需要。
    鉴权仍由 :func:`make_auth_middleware` 负责。
    """
    allowed = normalize_cors_origins(allow_origins)

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if not request.path.startswith(PROTECTED_PREFIX):
            return await handler(request)

        origin = request.headers.get("Origin")
        same_origin = bool(origin) and origin == f"{request.scheme}://{request.host}"
        cross_origin = bool(origin) and not same_origin
        origin_allowed = bool(origin) and (
            "*" in allowed or origin in allowed
        )
        if cross_origin and not origin_allowed:
            return web.json_response(
                {"ok": False, "errors": ["来源不在 web_panel.cors_origins 白名单内"]},
                status=403,
                dumps=_dumps,
            )

        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)

        if origin_allowed:
            headers = response.headers
            headers["Access-Control-Allow-Origin"] = "*" if "*" in allowed else origin
            headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Panel-Token"
            )
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Max-Age"] = "600"
            headers.add("Vary", "Origin")
        return response

    return cors_middleware


def _dumps(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "PROTECTED_PREFIX",
    "extract_token",
    "is_loopback_host",
    "make_auth_middleware",
    "make_cors_middleware",
    "normalize_cors_origins",
    "token_matches",
]
