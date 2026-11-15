"""网页面板的监听边界、token 与 CORS 白名单。"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from web.auth import make_cors_middleware, normalize_cors_origins
from web.server import WebServer


class _Settings:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_public_bind_without_token_is_rejected():
    settings = _Settings(
        {
            "web_panel.host": "0.0.0.0",
            "web_panel.port": 8390,
            "web_panel.token": "",
            "web_panel.cors_origins": [],
        }
    )
    with pytest.raises(ValueError, match="必须设置 token"):
        WebServer(settings=settings, static_dir=".")


def test_cors_origins_are_normalized_and_validated():
    assert normalize_cors_origins(
        ["https://panel.example.com/", "https://panel.example.com"]
    ) == ("https://panel.example.com",)
    assert normalize_cors_origins("*") == ("*",)
    with pytest.raises(ValueError):
        normalize_cors_origins(["panel.example.com"])


def _run_cors_checks():
    async def status(_request):
        return web.json_response({"ok": True})

    async def exercise():
        app = web.Application(
            middlewares=[
                make_cors_middleware(allow_origins=["https://panel.example.com"])
            ]
        )
        app.router.add_get("/api/status", status)
        async with TestClient(TestServer(app)) as client:
            allowed = await client.options(
                "/api/status",
                headers={
                    "Origin": "https://panel.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert allowed.status == 204
            assert (
                allowed.headers["Access-Control-Allow-Origin"]
                == "https://panel.example.com"
            )
            assert "Origin" in allowed.headers["Vary"]

            denied = await client.get(
                "/api/status", headers={"Origin": "https://evil.example.com"}
            )
            assert denied.status == 403
            assert "Access-Control-Allow-Origin" not in denied.headers

            same_origin = str(client.make_url("/")).rstrip("/")
            local = await client.get(
                "/api/status", headers={"Origin": same_origin}
            )
            assert local.status == 200

    asyncio.run(exercise())


def test_cors_allows_configured_origin_and_blocks_others():
    _run_cors_checks()
