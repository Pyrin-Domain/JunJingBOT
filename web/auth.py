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


def make_cors_middleware(*, allow_origin: str = "*"):
    """允许 ``frontend/`` 独立部署（python -m http.server）时跨源访问 ``/api``。

    只给 ``/api/*`` 加头；静态页面同源加载，不需要 CORS。
    鉴权仍由 :func:`make_auth_middleware` 负责。
    """

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if not request.path.startswith(PROTECTED_PREFIX):
            return await handler(request)

        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)

        headers = response.headers
        headers.setdefault("Access-Control-Allow-Origin", allow_origin)
        headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Panel-Token",
        )
        headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        headers.setdefault("Access-Control-Max-Age", "600")
        return response

    return cors_middleware


def _dumps(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "PROTECTED_PREFIX",
    "extract_token",
    "make_auth_middleware",
    "make_cors_middleware",
    "token_matches",
]
