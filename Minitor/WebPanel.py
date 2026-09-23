"""
WebPanel —— 网页版参数面板（改配置文件，不用改代码）

设计
----
- 后端用 aiohttp，默认和 Bot 跑在**同一个进程 / 事件循环**里：
  所以面板能直接读到运行状态（QQ 号、断点、心跳、Agent 是否加载）。
- 前端是 `webui/` 下的静态页面：无构建、无 CDN、离线可用。
- 保存 = 写回 `config/bot_config.json` + 调 `BOTMain.reload_config()` 热加载：
  关键词 / 搬运 / 提示词 / 回复文案 下一条消息就生效；
  只在启动时读的参数（agent.enable、web_panel.*）会返回 restart_required。
- 默认只绑 `127.0.0.1`；要在别的机器上访问，请设 `web_panel.token`，
  然后用 `http://主机:端口/?token=xxx` 打开。

HTTP 接口
---------
GET  /                    面板页面（静态文件）
GET  /static/<file>       静态资源
GET  /api/status          运行状态 + 配置文件信息 + 当前 WS 连接方式
GET  /api/ws              连接设置（config/config.json 的 type / host / port）
POST /api/ws              改连接设置（写 config/ 下的文件，重启 Bot 生效）
GET  /api/config          当前生效配置（默认值 ← 文件）+ 可选 action 名 + 图片目录
GET  /api/groups          群列表（给群号列表的下拉选择用，取 NapCat get_group_list）
POST /api/config          保存配置：{data: {...}}，先校验，再写盘 + 热加载
POST /api/validate        只校验不保存（表单实时提示用）
POST /api/reload          重新读盘并热加载

单独启动（不跑 Bot，只改参数）：
    python Minitor/WebPanel.py --port 8390
"""
import argparse
import json
import re
import time
from pathlib import Path

from aiohttp import web

try:  # 兼容 run.py 的扁平 import 和 `Minitor.WebPanel` 包内 import
    from NapCatTools import _IMGS_DIR
    from BotConfig import BotConfig, load_bot_config
    from Paths import Paths
    from Websockets import (MODE_FORWARD, MODE_REVERSE, NapCatBotConfig,
                            normalize_mode)
except ImportError:  # pragma: no cover
    from Minitor.NapCatTools import _IMGS_DIR
    from Minitor.BotConfig import BotConfig, load_bot_config
    from Minitor.Paths import Paths
    from Minitor.Websockets import (MODE_FORWARD, MODE_REVERSE,
                                   NapCatBotConfig, normalize_mode)


# 与 BOTMain._action_registry() 对应的关键词动作名（Bot 不在场时的兜底清单）
FALLBACK_ACTIONS = (
    "send_message",
    "send_img",
    "get_long_history_test",
    "set_tokenizer",
    "ocr",
    "export",
    "fabric",
    "get_music",
    "img_forward_create_task",
)

KNOWN_TOP_KEYS = (
    "master_qq",
    "heartbeat",
    "agent",
    "message",
    "auto_forward",
    "image_forward",
    "prompts",
    "replies",
    "keyword_actions",
    "web_panel",
)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ════════════════ 连接设置（config/config.json） ════════════════
# 面板只管这几个字段；文件里其它字段（如 forward_dedup）原样保留
WS_KEYS = (
    "type",
    "access_token",
    "ws_host",
    "ws_port",
    "ws_reverse_host",
    "ws_reverse_port",
    "ws_reverse_strict_token",
)


def _read_json_file(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Web] 读取 {path} 失败：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ws_section(raw: dict) -> dict:
    """从配置文件里挑出连接相关的字段。"""
    return {key: raw[key] for key in WS_KEYS if key in raw}


def validate_ws_config(data) -> tuple[list[str], dict]:
    """校验连接设置：返回 (errors, 整理好的字段)。"""
    if not isinstance(data, dict):
        return ["连接设置必须是 JSON 对象"], {}
    errors: list[str] = []
    cleaned: dict = {}

    raw_type = data.get("type")
    if raw_type is None or str(raw_type).strip() == "":
        cleaned["type"] = MODE_REVERSE
    else:
        mode = normalize_mode(raw_type, default="")
        if mode:
            cleaned["type"] = mode
        else:
            errors.append(f'type={raw_type!r} 不认识：反向填 "ws_re"，正向填 "ws"')

    if "access_token" in data and data["access_token"] is not None:
        cleaned["access_token"] = str(data["access_token"])
    for key in ("ws_host", "ws_reverse_host"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} 必须是非空字符串，例如 127.0.0.1")
            continue
        cleaned[key] = value.strip()
    for key in ("ws_port", "ws_reverse_port"):
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            port = int(value)
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是 1~65535 之间的整数")
            continue
        if not 1 <= port <= 65535:
            errors.append(f"{key} 必须在 1~65535 之间")
            continue
        cleaned[key] = port
    if data.get("ws_reverse_strict_token") is not None:
        cleaned["ws_reverse_strict_token"] = bool(data["ws_reverse_strict_token"])
    return errors, cleaned


# ════════════════════════════ 校验 ════════════════════════════
def _bool_field(node: dict, key: str, label: str, err):
    if key in node and not isinstance(node[key], bool):
        err(f"{label} 必须是 true / false")


def _int_list(value, label: str, err, warn):
    """群号 / QQ 号列表：允许 int 或纯数字字符串，其它一律报错。"""
    if value is None:
        return
    if not isinstance(value, list):
        err(f"{label} 必须是数组，例如 [123456, 234567]")
        return
    for i, item in enumerate(value):
        text = str(item).strip()
        if not text.isdigit():
            warn(f"{label}[{i}] = {item!r} 不是纯数字，运行时会忽略这一项")


def _bool_section(data: dict, section: str, label: str, err) -> dict:
    node = data.get(section)
    if node is None:
        return {}
    if not isinstance(node, dict):
        err(f"{label} 必须是对象")
        return {}
    return node


def validate_config(data: dict, imgs_dir: str = "") -> tuple[list[str], list[str]]:
    """校验一份配置：返回 (errors, warnings)。

    errors  —— 拦住保存（类型/格式不对，存下去会跑不起来）
    warnings —— 允许保存（未知 action、正则不合法……运行时会被跳过）
    """
    errors: list[str] = []
    warnings: list[str] = []

    def err(msg: str) -> None:
        errors.append(msg)

    def warn(msg: str) -> None:
        warnings.append(msg)

    if not isinstance(data, dict):
        return ["配置必须是一个 JSON 对象"], []

    # ── 主人 QQ ──
    master = data.get("master_qq")
    if master is None or not str(master).strip():
        err("master_qq 不能为空")
    elif not str(master).strip().isdigit():
        warn(f"master_qq={master!r} 不是纯数字，可能不是有效的 QQ 号")

    # ── 心跳 ──
    hb = data.get("heartbeat") or {}
    if not isinstance(hb, dict):
        err("heartbeat 必须是对象")
    else:
        low = hb.get("min_minutes")
        high = hb.get("max_minutes")
        for name, value in (("heartbeat.min_minutes", low), ("heartbeat.max_minutes", high)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                err(f"{name} 必须是正数（单位：分钟）")

    # ── 开关 ──
    agent = _bool_section(data, "agent", "agent", err)
    _bool_field(agent, "enable", "agent.enable", err)

    message = _bool_section(data, "message", "message", err)
    extra = message.get("extraconfig")
    if extra is not None and not isinstance(extra, dict):
        err("message.extraconfig 必须是对象")
    elif isinstance(extra, dict):
        _bool_field(extra, "tokenizer", "message.extraconfig.tokenizer", err)
        _bool_field(extra, "recent", "message.extraconfig.recent", err)

    # ── 搬运 ──
    auto = _bool_section(data, "auto_forward", "auto_forward", err)
    _bool_field(auto, "enable", "auto_forward.enable", err)
    _bool_field(auto, "dedup", "auto_forward.dedup", err)
    _int_list(auto.get("groupid_list"), "auto_forward.groupid_list", err, warn)
    _int_list(auto.get("target_groupid"), "auto_forward.target_groupid", err, warn)
    # 私聊搬运：userid_list（旧写法 user_id_list 也认）
    if "user_id_list" in auto and "userid_list" not in auto:
        warn("auto_forward.user_id_list 已改名为 userid_list，建议改名（旧名仍然生效）")
    _int_list(
        auto.get("userid_list", auto.get("user_id_list")),
        "auto_forward.userid_list",
        err,
        warn,
    )
    _int_list(auto.get("task_userid"), "auto_forward.task_userid", err, warn)
    from_bot = auto.get("auto_img_forward_from_bot")
    if from_bot is not None and not isinstance(from_bot, dict):
        err("auto_forward.auto_img_forward_from_bot 必须是对象")
    elif isinstance(from_bot, dict):
        _bool_field(from_bot, "enable", "auto_forward.auto_img_forward_from_bot.enable", err)
        # 旧写法：纯 QQ 号数组，等价于每个都是 type=all
        _int_list(
            from_bot.get("bot_id_list"),
            "auto_forward.auto_img_forward_from_bot.bot_id_list",
            err,
            warn,
        )
        # 新写法：每个机器人一个对象（bot_id / type / group_id / user_id_list）
        bot_list = from_bot.get("bot_list")
        if bot_list is not None and not isinstance(bot_list, list):
            err("auto_forward.auto_img_forward_from_bot.bot_list 必须是数组")
        elif isinstance(bot_list, list):
            for i, entry in enumerate(bot_list):
                tag = f"auto_forward.auto_img_forward_from_bot.bot_list[{i}]"
                if not isinstance(entry, dict):
                    err(f'{tag} 必须是对象，例如 {{"bot_id": 123456, "type": "group"}}')
                    continue
                if not str(entry.get("bot_id", "")).strip():
                    err(f"{tag}.bot_id 不能为空")
                elif not str(entry.get("bot_id", "")).strip().isdigit():
                    warn(f"{tag}.bot_id = {entry.get('bot_id')!r} 不是纯数字")
                scope = str(entry.get("type", "all") or "all").strip().lower()
                if scope not in ("group", "private", "all"):
                    err(f"{tag}.type 只能是 group / private / all（当前 {entry.get('type')!r}）")
                _int_list(entry.get("group_id"), f"{tag}.group_id", err, warn)
                _int_list(entry.get("user_id_list"), f"{tag}.user_id_list", err, warn)

    img_fwd = _bool_section(data, "image_forward", "image_forward", err)
    _int_list(img_fwd.get("auto_target_group_ids"), "image_forward.auto_target_group_ids", err, warn)

    # ── 提示词 / 回复文案 ──
    for section, label in (("prompts", "prompts"), ("replies", "replies")):
        node = data.get(section)
        if node is None:
            continue
        if not isinstance(node, dict):
            err(f"{label} 必须是对象")
            continue
        for key, value in node.items():
            if not isinstance(value, str):
                err(f"{label}.{key} 必须是字符串")

    history_regex = ((data.get("prompts") or {}).get("history_command_regex"))
    if isinstance(history_regex, str) and history_regex:
        try:
            re.compile(history_regex)
        except re.error as exc:
            err(f"prompts.history_command_regex 不是合法正则: {exc}")

    # ── 关键词 ──
    actions = data.get("keyword_actions")
    if actions is None:
        warn("keyword_actions 为空：关键词/命令都不会触发")
    elif not isinstance(actions, list):
        err("keyword_actions 必须是数组")
    else:
        for i, item in enumerate(actions):
            tag = f"keyword_actions[{i}]"
            if not isinstance(item, dict):
                err(f"{tag} 必须是对象")
                continue
            rx = item.get("regex")
            if not isinstance(rx, str) or not rx:
                err(f"{tag}.regex 不能为空")
            else:
                try:
                    re.compile(rx, re.I)
                except re.error as exc:
                    err(f"{tag}.regex 不是合法正则: {exc}")
            action = item.get("action")
            if action not in FALLBACK_ACTIONS:
                warn(f"{tag}.action={action!r} 未注册，运行时会跳过这条规则")
            params = item.get("params", {})
            if params is None:
                params = {}
                item["params"] = params
            if not isinstance(params, dict):
                err(f"{tag}.params 必须是对象")
                continue
            if action == "send_message" and not params.get("message"):
                warn(f"{tag}: send_message 需要 params.message（字符串或字符串数组）")
            if action == "send_img":
                addr = params.get("img_addr")
                if not isinstance(addr, str) or not addr:
                    err(f"{tag}: send_img 需要 params.img_addr")
                elif not addr.startswith(("/", "\\")) and ":" not in addr and "{imgs_dir}" not in addr:
                    warn(f"{tag}: img_addr={addr!r} 不是绝对路径（推荐用 {{imgs_dir}}/文件名）")

    # ── 面板自身 ──
    panel = _bool_section(data, "web_panel", "web_panel", err)
    _bool_field(panel, "enable", "web_panel.enable", err)
    port = panel.get("port")
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535)
    ):
        err("web_panel.port 必须是 1~65535 之间的整数")
    host = panel.get("host")
    if host is not None and not isinstance(host, str):
        err("web_panel.host 必须是字符串，例如 127.0.0.1")
    token = panel.get("token")
    if token is not None and not isinstance(token, str):
        err("web_panel.token 必须是字符串（留空表示不需要 token）")
    elif isinstance(host, str) and host not in ("127.0.0.1", "localhost", "::1") and not token:
        warn("面板监听非本机地址且没有设置 token，任何能访问该端口的人都能改参数")

    for key in data:
        if key.startswith("_"):
            continue
        if key not in KNOWN_TOP_KEYS:
            warn(f"未知参数段 {key!r}：Bot 不会读取，可能是拼写错误")

    return errors, warnings


# ════════════════════════════ 面板 ════════════════════════════
class WebPanel:
    """参数面板的 HTTP 服务（默认与 Bot 同进程）。"""

    def __init__(self, bot=None, config=None, host=None, port=None, token=None,
                 static_dir=None, connection=None):
        """
        :param bot: BOTMain 实例；为 None 时只能改参数、看不到运行状态
        :param config: BotConfig 实例（缺省用 bot.bot_config，再缺省读 bot_config.json）
        :param connection: 事件用的 NapCatConnection（run.py 建的那个）；
                           不传就从 bot.conn 取，只用于展示连接状态
        """
        self.bot = bot
        self._config = config if config is not None else getattr(bot, "bot_config", None)
        self.connection = connection if connection is not None else getattr(bot, "conn", None)
        # 群列表缓存（面板下拉框用，不用每次问 NapCat）
        self._group_cache: list = []
        self._group_cache_at: float = 0.0

        cfg = self.config
        self.host = host or str(cfg.get("web_panel.host", "127.0.0.1"))
        # 注意用 is not None：port=0 表示让系统随机分配可用端口（测试用）
        self.port = int(port if port is not None else cfg.get("web_panel.port", 8390))
        self.token = token if token is not None else (cfg.get("web_panel.token") or None)
        # 前端静态文件：webui/（老部署还在 Minitor/web_panel/ 也认）
        self.static_dir = Path(static_dir) if static_dir else Paths.webui_dir()

        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.app = self._build_app()

    # ── 配置对象 ──
    @property
    def config(self) -> BotConfig:
        if self._config is None:
            self._config = load_bot_config()
        return self._config

    def _known_actions(self) -> tuple[str, ...]:
        if self.bot is not None and hasattr(self.bot, "_action_registry"):
            try:
                return tuple(self.bot._action_registry().keys())
            except Exception:
                pass
        return FALLBACK_ACTIONS

    # ── 路由 ──
    def _build_app(self) -> web.Application:
        @web.middleware
        async def auth(request: web.Request, handler):
            if self.token and request.path.startswith("/api"):
                supplied = request.headers.get("X-Panel-Token") or request.query.get("token")
                if supplied != self.token:
                    return web.json_response(
                        {"ok": False, "errors": ["token 不正确"]}, status=401, dumps=_dumps
                    )
            return await handler(request)

        app = web.Application(middlewares=[auth])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/static/{name}", self._handle_static)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/groups", self._handle_groups)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_get("/api/ws", self._handle_get_ws)
        app.router.add_post("/api/ws", self._handle_save_ws)
        app.router.add_post("/api/config", self._handle_save_config)
        app.router.add_post("/api/validate", self._handle_validate)
        app.router.add_post("/api/reload", self._handle_reload)
        return app

    # ── 静态页面 ──
    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index = self.static_dir / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound(text=f"缺少面板文件：{index}")
        return web.FileResponse(index)

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        name = Path(request.match_info["name"]).name  # 防目录穿越
        path = self.static_dir / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ── 状态 ──
    def _config_meta(self) -> dict:
        path = Path(self.config.path)
        meta = {"path": str(path), "exists": path.exists()}
        if meta["exists"]:
            stat = path.stat()
            meta["size"] = stat.st_size
            meta["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        return meta

    def _check_point_summary(self) -> dict:
        out: dict = {}
        if self.bot is None:
            return out
        try:
            items = list(getattr(self.bot.mp, "check_point", {}).items())[:60]
        except Exception as exc:  # pragma: no cover
            return {"error": str(exc)}
        for group_id, event in items:
            if isinstance(event, dict):
                out[str(group_id)] = {
                    "message_id": event.get("message_id"),
                    "real_id": event.get("real_id"),
                    "user_id": event.get("user_id"),
                }
            else:
                out[str(group_id)] = {"raw": str(event)[:80]}
        return out

    async def _handle_status(self, request: web.Request) -> web.StreamResponse:
        payload = {
            "ok": True,
            "panel": {"url": self.url(), "host": self.host, "port": self.port,
                      "auth": bool(self.token)},
            "config": self._config_meta(),
            "connection": self._connection_info(),
            "bot_running": self.bot is not None,
        }
        if self.bot is not None:
            cfg = self.config
            heartbeat = getattr(self.bot, "_heartbeat_task", None)
            payload["bot"] = {
                "user_id": getattr(self.bot.mp, "user_id", None),
                "agent_enabled": cfg.agent_enabled,
                "agent_loaded": getattr(self.bot, "_agent", None) is not None,
                "campus_loaded": getattr(self.bot, "campus_assistant", None) is not None,
                "heartbeat_running": bool(heartbeat is not None and not heartbeat.done()),
                "keyword_count": len(cfg.keyword_actions),
                "auto_forward_enabled": cfg.auto_forward_enabled,
                "dedup_store_ready": bool(getattr(self.bot.forward, "_dedup_store", None)),
                "image_tasks": sorted(getattr(self.bot.ifs, "task_dict", {}).keys()),
                "check_points": self._check_point_summary(),
            }
        return web.json_response(payload, dumps=_dumps)

    # ── 连接方式（config/config.json） ──
    def _connection_info(self) -> dict:
        """当前 NapCat 连接方式 + 实际连接状态。"""
        info: dict = {"type": None, "label": None, "endpoint": None,
                      "connected": None, "config_path": None}
        try:
            cfg = NapCatBotConfig()
        except Exception as exc:   # 比如正向配置里没有 access_token
            info["error"] = str(exc)
            return info
        info.update({
            "type": cfg.type,
            "label": cfg.mode_label,
            "endpoint": cfg.endpoint,
            "host": cfg.mode_host,
            "port": cfg.mode_port,
            "config_path": str(cfg.path),
            "token_set": bool(cfg.token),
        })
        conn = self.connection
        if conn is not None:
            info["connected"] = bool(getattr(conn, "is_connected", False))
            info["role"] = getattr(conn, "role", None) or None
        # 正向模式下「发消息」是另一条连接
        api_conn = getattr(getattr(self.bot, "nc", None), "_rws", None)
        if api_conn is not None and api_conn is not conn:
            info["api_connected"] = bool(getattr(api_conn, "is_connected", False))
            api_cfg = getattr(api_conn, "config", None)
            info["api_endpoint"] = getattr(api_cfg, "endpoint", None)
        return info

    async def _handle_groups(self, request: web.Request) -> web.StreamResponse:
        """群列表（面板里选群号用）：向 NapCat 要 get_group_list，缓存 60 秒。"""
        now = time.monotonic()
        if self._group_cache and now - self._group_cache_at < 60:
            return web.json_response(
                {"ok": True, "groups": self._group_cache, "cached": True}, dumps=_dumps
            )
        conn = self.connection
        if conn is None:
            conn = getattr(getattr(self.bot, "nc", None), "_rws", None)
        if conn is None:
            return web.json_response(
                {"ok": True, "groups": [],
                 "error": "面板没跟 Bot 同一个进程，取不到群列表（手动填群号也行）"},
                dumps=_dumps,
            )
        try:
            resp = await conn.call_api("get_group_list", {})
        except Exception as exc:
            return web.json_response(
                {"ok": True, "groups": [], "error": f"取群列表失败：{exc}"}, dumps=_dumps
            )
        raw = resp.get("data") if isinstance(resp, dict) else None
        groups = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            gid = item.get("group_id")
            if gid is None:
                continue
            groups.append({
                "group_id": gid,
                "group_name": str(item.get("group_name") or gid),
            })
        self._group_cache = groups
        self._group_cache_at = now
        return web.json_response({"ok": True, "groups": groups}, dumps=_dumps)

    @staticmethod
    def _ws_path(name: str) -> Path:
        """写盘位置：现在在用的那份（config/ 优先）；没有就落到新布局 config/。"""
        current = Paths.config_file(name)
        return current if current.exists() else Paths.config_target(name)

    async def _handle_get_ws(self, request: web.Request) -> web.StreamResponse:
        cfg_path = self._ws_path("config.json")
        sender_path = self._ws_path("sender_config.json")
        return web.json_response(
            {
                "ok": True,
                "data": ws_section(_read_json_file(cfg_path)),
                "sender": ws_section(_read_json_file(sender_path)),
                "meta": {"config_path": str(cfg_path), "sender_path": str(sender_path)},
                "info": self._connection_info(),
            },
            dumps=_dumps,
        )

    async def _handle_save_ws(self, request: web.Request) -> web.StreamResponse:
        body = await self._read_body(request)
        data = self._extract_data(body)
        sender_data = body.get("sender") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return web.json_response(
                {"ok": False, "errors": ["请求体必须是 JSON 对象或 {data: {...}}"]},
                status=400, dumps=_dumps,
            )

        errors, cleaned = validate_ws_config(data)
        sender_cleaned: dict = {}
        if isinstance(sender_data, dict) and sender_data:
            sender_errors, sender_cleaned = validate_ws_config(sender_data)
            errors = errors + sender_errors
        if errors:
            return web.json_response(
                {"ok": False, "errors": errors}, status=400, dumps=_dumps
            )

        try:
            # 只改连接相关字段，文件里其它内容（如 forward_dedup）原样保留
            cfg_path = self._ws_path("config.json")
            merged = _read_json_file(cfg_path)
            merged.update(cleaned)
            _write_json_file(cfg_path, merged)
            written = [str(cfg_path)]
            if sender_cleaned:
                sender_path = self._ws_path("sender_config.json")
                smerged = _read_json_file(sender_path)
                smerged.update(sender_cleaned)
                _write_json_file(sender_path, smerged)
                written.append(str(sender_path))
        except OSError as exc:
            return web.json_response(
                {"ok": False, "errors": [f"写入失败：{exc}"]}, status=500, dumps=_dumps
            )
        return web.json_response(
            {
                "ok": True,
                "saved_to": written,
                "restart_required": True,
                "hint": "连接方式改动要重启 Bot 才生效（NapCat 那边也要填一样的端口）",
                "data": ws_section(_read_json_file(self._ws_path("config.json"))),
                "info": self._connection_info(),
            },
            dumps=_dumps,
        )

    # ── 配置读写 ──
    async def _handle_get_config(self, request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {
                "ok": True,
                "data": self.config.to_dict(),
                "meta": self._config_meta(),
                "actions": sorted(self._known_actions()),
                "imgs_dir": _IMGS_DIR,
            },
            dumps=_dumps,
        )

    @staticmethod
    async def _read_body(request: web.Request):
        try:
            return await request.json()
        except Exception:
            return None

    @staticmethod
    def _extract_data(body):
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    async def _handle_validate(self, request: web.Request) -> web.StreamResponse:
        body = await self._read_body(request)
        data = self._extract_data(body)
        if data is None:
            return web.json_response(
                {"ok": False, "errors": ["请求体不是合法 JSON"]}, status=400, dumps=_dumps
            )
        errors, warnings = validate_config(data, imgs_dir=_IMGS_DIR)
        return web.json_response(
            {"ok": not errors, "errors": errors, "warnings": warnings}, dumps=_dumps
        )

    async def _handle_save_config(self, request: web.Request) -> web.StreamResponse:
        body = await self._read_body(request)
        data = self._extract_data(body)
        if not isinstance(data, dict):
            return web.json_response(
                {"ok": False, "errors": ["请求体必须是 JSON 对象或 {data: {...}}"]},
                status=400,
                dumps=_dumps,
            )
        errors, warnings = validate_config(data, imgs_dir=_IMGS_DIR)
        if errors:
            return web.json_response(
                {"ok": False, "errors": errors, "warnings": warnings}, status=400, dumps=_dumps
            )
        try:
            # 用默认值补齐缺失字段再写盘，保证文件始终完整
            merged = BotConfig(path=self.config.path, overrides=data)
            saved_to = merged.save()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "errors": [f"写入失败: {exc}"]}, status=500, dumps=_dumps
            )

        result: dict = {"saved_to": saved_to}
        if self.bot is not None and hasattr(self.bot, "reload_config"):
            try:
                result.update(self.bot.reload_config())
            except Exception as exc:  # pragma: no cover
                result["reload_error"] = str(exc)
        else:
            self.config.reload()
            result["applied"] = ["（未连接 Bot，参数已写入文件）"]
            result["restart_required"] = True
        return web.json_response(
            {"ok": True, "warnings": warnings, "result": result, "data": self.config.to_dict(),
             "meta": self._config_meta()},
            dumps=_dumps,
        )

    async def _handle_reload(self, request: web.Request) -> web.StreamResponse:
        if self.bot is not None and hasattr(self.bot, "reload_config"):
            result = self.bot.reload_config()
        else:
            self.config.reload()
            result = {"applied": ["（未连接 Bot）"], "restart_required": True}
        return web.json_response(
            {"ok": True, "result": result, "data": self.config.to_dict(),
             "meta": self._config_meta()},
            dumps=_dumps,
        )

    # ── 生命周期 ──
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        token = f"?token={self.token}" if self.token else ""
        return f"http://{host}:{self.port}/{token}"

    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def start_if_enabled(self) -> bool:
        """按 bot_config.json 的 web_panel 段决定是否启动；失败不影响 Bot 运行。"""
        if not bool(self.config.get("web_panel.enable", True)):
            print("[Web] bot_config.json 里 web_panel.enable=false，未启动参数面板")
            return False
        try:
            await self.start()
        except OSError as exc:
            print(f"[Web] 参数面板启动失败（{self.host}:{self.port}）：{exc}")
            return False
        print(f"[Web] 参数面板已启动：{self.url()}")
        return True


def main(argv=None) -> None:
    """脱离 Bot 单独启动面板（只改参数，看不到运行状态）。"""
    parser = argparse.ArgumentParser(description="君景 Bot 参数面板（可脱离 Bot 单独运行）")
    parser.add_argument("--config", default=None, help="bot_config.json 路径")
    parser.add_argument("--host", default=None, help="监听地址，默认取配置文件")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取配置文件")
    parser.add_argument("--token", default=None, help="访问 token，默认取配置文件")
    parser.add_argument("--static-dir", default=None, help="静态页面目录")
    args = parser.parse_args(argv)

    cfg = load_bot_config(args.config)
    panel = WebPanel(
        bot=None,
        config=cfg,
        host=args.host,
        port=args.port,
        token=args.token,
        static_dir=args.static_dir,
    )
    print(f"[Web] 参数面板（无 Bot 模式）：{panel.url()}")
    print("[Web] Ctrl+C 退出")
    web.run_app(panel.app, host=panel.host, port=panel.port, print=None)


if __name__ == "__main__":
    main()
