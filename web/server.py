"""网页面板的 aiohttp 服务。

职责只有三件：托管 ``frontend/`` 静态文件、装配 API 路由、管理生命周期。
业务规则在 ``bot/``，配置读写在 ``config/``；这里只做 HTTP ↔ 对象适配。

``bot`` 是鸭子类型对象（可以是 ``None``）：

- ``bot.settings``           :class:`config.settings.BotSettings`
- ``bot.reload_config()``    热加载（同步或异步都接受）
- ``bot.mp.check_point``     断点 dict
- ``bot.events_connection`` / ``bot.conn``  事件连接
- ``bot.action_registry()``  action 名 → 处理函数
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from config import BotSettings, load_settings

from .auth import make_auth_middleware, make_cors_middleware
from .routes import WEB_SERVER_KEY, register_routes
from .validation import FALLBACK_ACTIONS

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8390


def _as_port(value: Any, default: int = DEFAULT_PORT) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WebServer:
    """参数面板 HTTP 服务（默认与 Bot 同进程、同事件循环）。"""

    def __init__(
        self,
        bot=None,
        settings: BotSettings | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        static_dir: str | Path | None = None,
    ):
        self.bot = bot

        if settings is None:
            settings = getattr(bot, "settings", None)
        if settings is None:
            settings = load_settings()
        self.settings: BotSettings = settings

        cfg_host = self.settings.get("web_panel.host", DEFAULT_HOST)
        cfg_port = self.settings.get("web_panel.port", DEFAULT_PORT)
        cfg_token = self.settings.get("web_panel.token") or None

        self.host = str(host if host is not None else (cfg_host or DEFAULT_HOST))
        # port=0 表示让系统随机分配（测试用），所以必须用 is not None 判断
        self.port = _as_port(port if port is not None else cfg_port)
        self.token = (
            str(token) if token is not None else (str(cfg_token) if cfg_token else None)
        )
        self.static_dir = Path(static_dir) if static_dir else self.settings.paths.frontend_dir

        # 群列表缓存（面板下拉框用，避免每次问 NapCat）
        self._group_cache: list[dict] = []
        self._group_cache_at: float = 0.0

        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._started = False
        self.app = self._build_app()

    # ════════════════════════ 构造 ════════════════════════
    def _build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[make_cors_middleware(), make_auth_middleware(self.token)]
        )
        app[WEB_SERVER_KEY] = self

        app.router.add_get("/", self._handle_index)
        # 新结构：/static/js/app.js → frontend/assets/js/app.js
        # 旧引用：/static/style.css → frontend/assets/style.css
        app.router.add_get("/static/{tail:.*}", self._handle_static)
        # 前端独立部署时 index.html 用相对路径 assets/...，后端也接住
        app.router.add_get("/assets/{tail:.*}", self._handle_static)
        app.router.add_get("/favicon.ico", self._handle_favicon)

        register_routes(app)
        return app

    # ════════════════════════ 静态文件 ════════════════════════
    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        candidates = [
            self.static_dir / "index.html",
            self.static_dir.parent / "index.html",
        ]
        for index in candidates:
            if index.is_file():
                return web.FileResponse(index)
        raise web.HTTPNotFound(text=f"缺少面板文件：{candidates[0]}")

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        rel = request.match_info.get("tail", "")
        path = self._resolve_asset(rel)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _handle_favicon(self, request: web.Request) -> web.StreamResponse:
        return web.Response(status=204)

    def _asset_roots(self) -> list[Path]:
        roots: list[Path] = []
        assets = self.static_dir / "assets"
        if assets.is_dir():
            roots.append(assets)
        roots.append(self.static_dir)
        return roots

    def _resolve_asset(self, rel: str) -> Path | None:
        """把 URL 里的相对路径安全地映射到静态目录（防目录穿越）。"""
        rel = (rel or "").strip().lstrip("/\\")
        if not rel:
            return None
        parts = Path(rel).parts
        if any(part in ("..", "") for part in parts) or Path(rel).is_absolute():
            return None
        for root in self._asset_roots():
            try:
                base = root.resolve()
                candidate = (base / rel).resolve()
            except OSError:  # pragma: no cover - 极端路径
                continue
            if candidate == base or base not in candidate.parents:
                continue
            if candidate.is_file():
                return candidate
        return None

    # ════════════════════════ 地址 ════════════════════════
    @property
    def display_host(self) -> str:
        return "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host

    @property
    def public_url(self) -> str:
        """不含 token，可安全放进 ``/api/status``。"""
        return f"http://{self.display_host}:{self.port}/"

    @property
    def url(self) -> str:
        """带 token 的访问地址（只用于启动日志 / 终端展示）。"""
        suffix = f"?token={quote(self.token, safe='')}" if self.token else ""
        return f"http://{self.display_host}:{self.port}/{suffix}"

    # ════════════════════════ bot 适配 ════════════════════════
    @property
    def imgs_dir(self) -> str:
        return str(self.settings.paths.images_dir)

    def _first_attr(self, *names: str):
        for name in names:
            value = getattr(self.bot, name, None)
            if value is not None:
                return value
        return None

    @property
    def events_connection(self):
        """收事件的那条连接（反向模式同时负责发 API）。"""
        conn = self._first_attr("events_connection", "conn", "connection")
        if conn is not None and (
            hasattr(conn, "call_api") or hasattr(conn, "is_connected")
        ):
            return conn
        return None

    @property
    def api_connection(self):
        """发 API 的那条连接；反向模式下与事件连接相同。"""
        conn = self._first_attr("sender_connection", "api_connection")
        if conn is None:
            # 旧结构兜底：BotMain.nc._rws
            legacy = getattr(self.bot, "nc", None)
            conn = getattr(legacy, "_rws", None) if legacy is not None else None
        if conn is not None and conn is not self.events_connection:
            return conn
        return None

    def action_names(self) -> list[str]:
        """Bot 注册的 action 名；Bot 不在场时回落到旧面板的兜底清单。"""
        registry = getattr(self.bot, "action_registry", None)
        data = None
        if callable(registry):
            try:
                data = registry()
            except Exception:
                data = None
        elif isinstance(registry, dict):
            data = registry

        if not isinstance(data, dict):
            legacy = getattr(self.bot, "_action_registry", None)
            if callable(legacy):
                try:
                    data = legacy()
                except Exception:
                    data = None
            elif isinstance(legacy, dict):
                data = legacy

        if isinstance(data, dict) and data:
            return sorted(str(key) for key in data)
        return sorted(FALLBACK_ACTIONS)

    async def reload_bot(self) -> dict:
        """调用 ``bot.reload_config()``；同步 / 异步实现都接受。"""
        if self.bot is None:
            self.settings.reload()
            return {
                "applied": ["（未连接 Bot，参数已写入文件）"],
                "restart_required": True,
                "restart_reason": "面板在独立模式下运行",
            }

        handler = getattr(self.bot, "reload_config", None)
        if not callable(handler):
            self.settings.reload()
            return {
                "applied": ["（Bot 未实现 reload_config，已重新读盘）"],
                "restart_required": True,
            }

        result = handler()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return dict(result)
        if result is None:
            return {}
        return {"applied": result if isinstance(result, list) else [str(result)]}

    # ════════════════════════ 状态 ════════════════════════
    def config_meta(self) -> dict:
        path = Path(self.settings.path)
        meta: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "layout": self.settings.layout_report(),
        }
        if meta["exists"]:
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover
                pass
            else:
                meta["size"] = stat.st_size
                meta["mtime"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                )
        return meta

    def connection_info(self) -> dict:
        """当前连接配置 + 实际连接状态。"""
        info: dict[str, Any] = {
            "type": None,
            "label": None,
            "backend": None,
            "backend_label": None,
            "history_anchor": None,
            "endpoint": None,
            "connected": None,
            "config_path": None,
        }
        try:
            conn_cfg = self.settings.connection
        except Exception as exc:  # 配置损坏时不要让状态页 500
            info["error"] = str(exc)
            return info

        info.update(
            {
                "type": conn_cfg.type,
                "label": conn_cfg.mode_label,
                "backend": conn_cfg.backend,
                "backend_label": conn_cfg.backend_label,
                "history_anchor": conn_cfg.history_anchor,
                "endpoint": conn_cfg.endpoint,
                "host": conn_cfg.mode_host,
                "port": conn_cfg.mode_port,
                "config_path": str(conn_cfg.path),
                "token_set": bool(conn_cfg.access_token),
            }
        )

        events_conn = self.events_connection
        if events_conn is not None:
            info["connected"] = bool(getattr(events_conn, "is_connected", False))
            role = getattr(events_conn, "role", None)
            if role:
                info["role"] = role
            describe = getattr(events_conn, "describe", None)
            if callable(describe):
                try:
                    info["description"] = str(describe())
                except Exception:  # pragma: no cover
                    pass

        api_conn = self.api_connection
        if api_conn is not None:
            info["api_connected"] = bool(getattr(api_conn, "is_connected", False))
            api_cfg = getattr(api_conn, "config", None)
            info["api_endpoint"] = getattr(api_cfg, "endpoint", None)
        return info

    def checkpoint_summary(self) -> dict:
        if self.bot is None:
            return {}
        try:
            mp = getattr(self.bot, "mp", None)
            check_point = getattr(mp, "check_point", {}) if mp is not None else {}
            items = list(dict(check_point or {}).items())[:60]
        except Exception as exc:  # pragma: no cover
            return {"error": str(exc)}

        summary: dict[str, Any] = {}
        for group_id, event in items:
            if isinstance(event, dict):
                summary[str(group_id)] = {
                    "message_id": event.get("message_id"),
                    "message_seq": event.get("message_seq"),
                    "real_id": event.get("real_id"),
                    "user_id": event.get("user_id"),
                }
            else:
                summary[str(group_id)] = {"raw": str(event)[:80]}
        return summary

    def _bot_payload(self) -> dict:
        settings = self.settings
        check_points = self.checkpoint_summary()
        checkpoint_count = len(
            [key for key in check_points if key != "error"]
        )
        agent = self._first_attr("agent", "_agent")
        heartbeat = getattr(self.bot, "_heartbeat_task", None)
        try:
            heartbeat_running = bool(getattr(self.bot, "heartbeat_running", False)) or bool(
                heartbeat is not None and not heartbeat.done()
            )
        except AttributeError:  # 某些实现把 _heartbeat_task 存成 bool
            heartbeat_running = bool(getattr(self.bot, "heartbeat_running", False))
        forward = getattr(self.bot, "forward", None)
        range_tasks_obj = getattr(self.bot, "range_tasks", None)
        task_dict = getattr(range_tasks_obj, "task_dict", {}) or {}
        mp = getattr(self.bot, "mp", None)
        auto_group_ids = settings.get("auto_forward.groupid_list", []) or []

        return {
            "user_id": getattr(mp, "user_id", None),
            "agent_enabled": settings.agent_enabled,
            "agent_loaded": agent is not None,
            "campus_loaded": getattr(self.bot, "campus_assistant", None) is not None,
            "heartbeat_running": bool(
                getattr(self.bot, "heartbeat_running", False)
                or (heartbeat is not None and not heartbeat.done())
            ),
            "keyword_count": len(settings.keyword_actions),
            "auto_forward_enabled": settings.auto_forward_enabled,
            "strip_tail_ads": {
                "enable": bool((settings.auto_forward_kwargs.get("strip_tail_ads") or {}).get("enable")),
                "scan_tail": (settings.auto_forward_kwargs.get("strip_tail_ads") or {}).get("scan_tail"),
                "append": len(
                    (
                        (settings.auto_forward_kwargs.get("strip_tail_ads") or {}).get("append") or {}
                    ).get("items")
                    or []
                ),
            },
            "dedup_store_ready": bool(
                getattr(forward, "_dedup_store", None)
                or getattr(forward, "dedup", None)
            ),
            "range_tasks": sorted(
                f"{gid}:{task.get('command')}"
                for gid, task in task_dict.items()
                if isinstance(task, dict)
            ),
            "check_points": check_points,
            "checkpoint_count": checkpoint_count,
            "group_count": len(auto_group_ids) if isinstance(auto_group_ids, list) else 0,
            "known_groups": len(self._group_cache),
        }

    def status_payload(self) -> dict:
        info = self.connection_info()
        payload: dict[str, Any] = {
            "ok": True,
            "running": True,
            "panel": {
                # 只给不带 token 的地址：旧版 /api/status 泄露 token，这里修掉
                "url": self.public_url,
                "host": self.host,
                "port": self.port,
                "auth": bool(self.token),
            },
            "config": self.config_meta(),
            "connection": info,
            "ws": {
                "type": info.get("type"),
                "label": info.get("label"),
                "backend": info.get("backend"),
                "backend_label": info.get("backend_label"),
                "history_anchor": info.get("history_anchor"),
                "endpoint": info.get("endpoint"),
                "config_path": info.get("config_path"),
                "token_set": info.get("token_set", False),
            },
            "bot_running": self.bot is not None,
        }
        if self.bot is not None:
            payload["bot"] = self._bot_payload()
        return payload

    # ════════════════════════ 生命周期 ════════════════════════
    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        self._started = True
        # port=0（测试）时把真实端口写回来
        server = getattr(self.site, "_server", None)
        sockets = getattr(server, "sockets", None) if server is not None else None
        if sockets:
            try:
                self.port = int(sockets[0].getsockname()[1])
            except (OSError, IndexError, TypeError):  # pragma: no cover
                pass

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = None
        self.site = None
        self._started = False

    async def start_if_enabled(self) -> bool:
        """按 ``web_panel.enable`` 启动；失败只打印，不影响 Bot。"""
        if not bool(self.settings.get("web_panel.enable", True)):
            print("[Web] bot_config.json 里 web_panel.enable=false，未启动参数面板")
            return False
        try:
            await self.start()
        except OSError as exc:
            print(f"[Web] 参数面板启动失败（{self.host}:{self.port}）：{exc}")
            return False
        print(f"[Web] 参数面板已启动：{self.url}")
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"WebServer(host={self.host!r}, port={self.port!r}, bot={self.bot is not None})"


__all__ = ["WebServer", "DEFAULT_HOST", "DEFAULT_PORT"]
