"""
NapCat WebSocket 连接层（正向 / 反向统一实现）

NapCat 有两种接法，由配置里的 ``type`` 字段决定（新布局：``config/config.json``）：

    "type": "ws_re"   反向：Bot 起 WS 服务器，NapCat 主动连过来；同一条连接
                      既收事件推送，又发 API 请求（用 echo 配对）。
    "type": "ws"      正向：Bot 主动连 NapCat 的 WS 服务器收事件；API 请求走
                      sender_config.json 那条连接（run.py 里另外建一个
                      NapCatConnection，两条连接互不干扰）。

两种模式对外接口完全一样，上层（BOTMain / minitor.py / NapCatAPI.py）不用关心是哪种：

    conn = NapCatConnection(NapCatBotConfig())     # 读 config/config.json
    await conn.start()
    conn.set_event_handler(on_event)               # 事件回调（可 async）
    await conn.wait_for_connect()                  # 等 NapCat 连上
    resp = await conn.call_api("get_login_info")   # 发 API 并等响应

配置文件示例::

    // 反向：Bot 监听，NapCat 连过来
    {"type": "ws_re", "access_token": "NapCat520",
     "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 8085}

    // 正向：Bot 连 NapCat
    {"type": "ws", "access_token": "NapCat520",
     "ws_host": "127.0.0.1", "ws_port": 3001}

兼容说明：

- 没写 ``type`` 时按反向 ``ws_re`` 处理，并打印一行提示；
- 旧名字 ``NapCatReverseWS`` 仍然可用（就是 ``NapCatConnection``）；
- ``gettoken()`` 保留（旧代码按文件名读配置）。
"""
import asyncio
import inspect
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets

try:  # 兼容 run.py 的扁平 import 和 `Minitor.Websockets` 包内 import
    from Paths import Paths
except ImportError:  # pragma: no cover
    from Minitor.Paths import Paths


# ──────────────────────────── 模式与默认值 ────────────────────────────
MODE_FORWARD = "ws"       # 正向：Bot 主动连 NapCat
MODE_REVERSE = "ws_re"    # 反向：Bot 起服务器等 NapCat 连过来
DEFAULT_MODE = MODE_REVERSE

_FORWARD_ALIASES = {"ws", "ws_forward", "forward", "client", "正向"}
_REVERSE_ALIASES = {"ws_re", "ws_reverse", "ws_rev", "reverse", "server", "反向"}

DEFAULT_FORWARD_HOST = "127.0.0.1"
DEFAULT_FORWARD_PORT = 3001
DEFAULT_REVERSE_HOST = "127.0.0.1"
DEFAULT_REVERSE_PORT = 8085

DEFAULT_RETRY_SECONDS = 5.0    # 正向连接断开后的重试间隔
DEFAULT_CALL_TIMEOUT = 30.0    # call_api 等响应的超时
_PING_INTERVAL = 30.0
_PING_TIMEOUT = 10.0


def normalize_mode(value, *, default: str = DEFAULT_MODE) -> str:
    """把配置里的 ``type`` 归一化成 ``ws`` / ``ws_re``。"""
    if value is None:
        return default
    key = str(value).strip().lower()
    if key in _FORWARD_ALIASES:
        return MODE_FORWARD
    if key in _REVERSE_ALIASES:
        return MODE_REVERSE
    if default:
        print(f"[WS] 无法识别的 type={value!r}，按 {default} 处理（可用 ws / ws_re）")
    else:
        print(f"[WS] 无法识别的 type={value!r}（可用 ws / ws_re）")
    return default


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WS] 读取 {path} 失败：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_config_path(config_file=None) -> Path:
    """文件名 → config/ 目录（兼容老根目录）；带目录或绝对路径原样使用。"""
    if config_file is None:
        return Paths.config_file("config.json")
    return Paths.config_file(str(config_file))


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_token(ws) -> str:
    """从连上来的 WS 请求里取 access_token（query 优先，其次 Authorization 头）。"""
    request = getattr(ws, "request", None)
    path = None
    for obj in (request, ws):
        if obj is None:
            continue
        value = getattr(obj, "path", None)
        if isinstance(value, str) and value:
            path = value
            break
    if path:
        query = parse_qs(urlsplit(path).query)
        token = (query.get("access_token") or [""])[0]
        if token:
            return token
    headers = getattr(request, "headers", None) if request is not None else None
    if headers is None:
        headers = getattr(ws, "request_headers", None)
    if headers:
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        auth = str(auth).strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if auth:
            return auth
    return ""


def gettoken(file_path="config.json") -> dict:
    """旧接口：返回 ``{'token', 'port', 'host'}``（正向连接用）。"""
    path = _resolve_config_path(file_path)
    raw = _load_json(path)
    token = raw.get("access_token")
    if not token:
        raise ValueError(f"{path} 中未找到 access_token")
    return {
        "token": token,
        "port": _as_int(raw.get("ws_port"), DEFAULT_FORWARD_PORT),
        "host": raw.get("ws_host", DEFAULT_FORWARD_HOST),
    }


# ──────────────────────────── 配置 ────────────────────────────
class NapCatBotConfig:
    """一个 NapCat 连接的配置（config/config.json、config/sender_config.json…）。

    ``type`` 决定模式：

    - ``"ws_re"``（默认）反向：监听 ``ws_reverse_host`` / ``ws_reverse_port``
    - ``"ws"``   正向：主动连 ``ws_host`` / ``ws_port``

    两种模式都保留对方的字段，所以网页面板可以随时改 type 而不用重写文件。
    """

    def __init__(self, config_file=None, *, required: bool = False):
        self.path = _resolve_config_path(config_file)
        raw = _load_json(self.path)
        if raw.get("type") is None:
            print(
                f"[WS] {self.path} 里没有 type 字段，按反向 ws_re 处理"
                '（建议补上 "type": "ws_re" 或 "ws"）'
            )
        self._apply_raw(raw, required=required)

    # ── 构造 ──
    @classmethod
    def from_dict(cls, raw: dict, *, mode: str | None = None,
                  path=None, required: bool = False) -> "NapCatBotConfig":
        """直接用 dict 造配置（测试用；也可给内存里的配置）。"""
        obj = cls.__new__(cls)
        raw = dict(raw or {})
        if mode is not None and raw.get("type") is None:
            raw["type"] = mode
        obj.path = Path(path) if path is not None else Path("<dict>")
        obj._apply_raw(raw, required=required)
        return obj

    def _apply_raw(self, raw: dict, *, required: bool = False) -> None:
        self._raw_config = raw
        self.type = normalize_mode(raw.get("type"))
        self.token = str(raw.get("access_token") or "")

        # 正向
        self.host = raw.get("ws_host", DEFAULT_FORWARD_HOST)
        self.port = _as_int(raw.get("ws_port"), DEFAULT_FORWARD_PORT)
        # 反向
        self.ws_reverse_host = raw.get("ws_reverse_host", DEFAULT_REVERSE_HOST)
        self.ws_reverse_port = _as_int(raw.get("ws_reverse_port"), DEFAULT_REVERSE_PORT)
        # 反向是否严格校验 token（默认关：NapCat 有时不带 token 连过来）
        self.strict_token = bool(raw.get("ws_reverse_strict_token", False))

        if not self.token:
            if required or self.is_forward:
                raise ValueError(f"{self.path} 中未找到 access_token")
            print("[WS] 警告：配置里没有 access_token，反向连接不做校验")

        # 兼容旧字段
        self.info = {"token": self.token, "port": self.port, "host": self.host}
        self.WS_URL = self._build_ws_url()

    def _build_ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}?access_token={self.token}"

    # ── 模式 ──
    @property
    def is_reverse(self) -> bool:
        return self.type == MODE_REVERSE

    @property
    def is_forward(self) -> bool:
        return self.type == MODE_FORWARD

    @property
    def mode_host(self) -> str:
        return self.ws_reverse_host if self.is_reverse else self.host

    @property
    def mode_port(self) -> int:
        return self.ws_reverse_port if self.is_reverse else self.port

    @property
    def mode_label(self) -> str:
        return "反向(ws_re)" if self.is_reverse else "正向(ws)"

    @property
    def endpoint(self) -> str:
        """当前模式用的地址（不含 token，方便打印 / 面板显示）。"""
        return f"ws://{self.mode_host}:{self.mode_port}"

    def describe(self) -> str:
        if self.is_reverse:
            return f"反向 ws_re：监听 {self.endpoint}，等 NapCat 连过来"
        return f"正向 ws：连接 {self.endpoint}"

    def raw(self) -> dict:
        return dict(self._raw_config)


# ──────────────────────────── 连接 ────────────────────────────
class NapCatConnection:
    """正向 / 反向通用的 NapCat 连接。

    一条连接同时负责「收事件」和「发 API」（正向模式下 API 连接由 run.py
    用另一个实例建，两条连接互不干扰）。
    """

    def __init__(self, config=None, *, role: str = "", mode: str | None = None):
        self.config = _coerce_config(config, mode=mode)
        self.role = role or ""
        self._label = f"（{self.role}）" if self.role else ""

        # 连接对象
        self._ws = None                    # 当前连接（两种模式共用）
        self._server = None                # 反向：WS 服务器
        self._connect_task = None          # 正向：连接 / 重连任务
        self._reader_task = None           # 兼容旧代码的字段（本实现不使用）
        self._started = False
        self._closing = False
        self._bg_tasks: set[asyncio.Task] = set()

        # 事件分发 / API 调用
        self._event_handler: Callable[[dict], Any] | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()

    # ── 属性 ──
    @property
    def mode(self) -> str:
        return self.config.type

    @property
    def is_reverse(self) -> bool:
        return self.config.is_reverse

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    def describe(self) -> str:
        return f"{self.config.mode_label}{self._label} {self.config.endpoint}"

    # ── 生命周期 ──
    async def start(self) -> "NapCatConnection":
        """反向：起服务器；正向：后台连过去（失败自动重试）。"""
        if self._started:
            return self
        self._started = True
        self._closing = False
        if self.is_reverse:
            host, port = self.config.ws_reverse_host, self.config.ws_reverse_port
            self._server = await websockets.serve(
                self._on_client,
                host,
                port,
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
            )
            print(
                f"[WS] 反向服务器已启动 ws://{host}:{port}{self._label}，等待 NapCat 连接…"
            )
        else:
            self._connect_task = asyncio.create_task(self._connect_loop())
            print(f"[WS] 正向模式：连接 {self.config.endpoint}{self._label} …")
        return self

    async def stop(self) -> None:
        self._closing = True
        if self._connect_task is not None:
            self._connect_task.cancel()
            await _swallow(self._connect_task)
            self._connect_task = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await _swallow(self._reader_task)
            self._reader_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            await _swallow(task)
        self._bg_tasks.clear()

        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        self._connected.clear()
        self._started = False
        self._fail_pending("连接已关闭")
        print(f"[WS] 已关闭{self._label}")

    # ── 反向：NapCat 连上来 ──
    async def _on_client(self, ws) -> None:
        if not self._check_token(ws):
            print(f"[WS] 拒绝连接：access_token 不匹配 {self._peer(ws)}")
            try:
                await ws.close(4001, "token 不匹配")
            except Exception:
                pass
            return

        old = self._ws
        if old is not None and old is not ws:
            self._fail_pending("NapCat 重新连接，旧请求已作废")
            self._spawn(_close_quietly(old))

        self._ws = ws
        self._connected.set()
        print(f"[WS] NapCat 已连接（反向 ws_re）{self._label} {self._peer(ws)}")
        try:
            await self._read_loop(ws)
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()
                self._fail_pending("NapCat 连接已断开")
            print(f"[WS] NapCat 连接已断开{self._label}")

    # ── 正向：主动连 NapCat（断线自动重连） ──
    async def _connect_loop(self) -> None:
        url = self.config.WS_URL
        while not self._closing:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    proxy=None,          # 本机连接不走系统代理
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    print(f"[WS] 已连接 NapCat（正向 ws）{self._label} {self.config.endpoint}")
                    try:
                        await self._read_loop(ws)
                    finally:
                        if self._ws is ws:
                            self._ws = None
                        self._connected.clear()
                        self._fail_pending("NapCat 连接已断开")
                        print(f"[WS] NapCat 连接已断开{self._label}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ws = None
                self._connected.clear()
                if self._closing:
                    return
                print(
                    f"[WS] 正向连接失败{self._label}：{exc!r}"
                    f"，{DEFAULT_RETRY_SECONDS:g} 秒后重试"
                )
            if self._closing:
                return
            try:
                await asyncio.sleep(DEFAULT_RETRY_SECONDS)
            except asyncio.CancelledError:
                raise

    # ── 读 / 分发 ──
    async def _read_loop(self, ws) -> None:
        try:
            async for raw in ws:
                data = _decode(raw)
                if data is not None:
                    self._dispatch(data)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:
            print(f"[WS] 读取异常{self._label}：{exc!r}")

    def _dispatch(self, data: dict) -> None:
        echo = data.get("echo")
        if echo is not None and echo in self._pending:
            future = self._pending.pop(echo)
            if not future.done():
                future.set_result(data)
            return
        handler = self._event_handler
        if handler is None:
            return
        # 事件不阻塞 reader
        self._spawn(self._safe_event(handler, data))

    async def _safe_event(self, handler, data: dict) -> None:
        try:
            result = handler(data)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WS] 事件处理异常：{exc!r}")

    # ── token 校验 ──
    def _check_token(self, ws) -> bool:
        token = self.config.token
        if not token:
            return True
        if _extract_token(ws) == token:
            return True
        if self.config.strict_token:
            return False
        print("[WS] 警告：连上来的 access_token 与配置不一致"
              "（ws_reverse_strict_token=false，先放行）")
        return True

    @staticmethod
    def _peer(ws) -> str:
        addr = getattr(ws, "remote_address", None)
        if isinstance(addr, tuple) and len(addr) >= 2:
            return f"{addr[0]}:{addr[1]}"
        return str(addr or "?")

    # ── 对外 API ──
    def set_event_handler(self, handler: Callable[[dict], Any]) -> "NapCatConnection":
        """注册事件处理器（同步 / 异步都行）。"""
        self._event_handler = handler
        return self

    async def wait_for_connect(self, timeout: float | None = None) -> bool:
        """等 NapCat 连上来；timeout 为 None 时一直等。返回是否等到了。"""
        if timeout is None:
            await self._connected.wait()
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def ensure_connected(self, timeout: float | None = None) -> "NapCatConnection":
        """没启动就先启动，没连上就等一下（面板 / 心跳之类的懒加载用）。"""
        if not self._started:
            await self.start()
        if not self.is_connected and not await self.wait_for_connect(timeout):
            raise TimeoutError(f"等待 NapCat 连接超时（{self.describe()}）")
        return self

    async def call_api(self, action: str, params: dict | None = None,
                       timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
        """发一条 API 请求并等 NapCat 的响应（echo 配对）。

        Raises:
            RuntimeError: 还没连上 NapCat
            TimeoutError: timeout 秒内没收到响应（默认 30 秒）
        """
        if self._ws is None:
            raise RuntimeError(
                f"NapCat 未连接（{self.config.mode_label}{self._label}），无法调用 {action}；"
                f"请确认 {self.config.path} 里的 type / 端口，以及 NapCat 端的 WebSocket 配置"
            )
        if params is None:
            params = {}

        echo_id = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo_id}
        future = asyncio.get_running_loop().create_future()
        self._pending[echo_id] = future
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{action} 在 {timeout:g} 秒内没有响应") from None
        finally:
            self._pending.pop(echo_id, None)

    # ── 内部小工具 ──
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))


def _coerce_config(config, *, mode: str | None = None) -> NapCatBotConfig:
    """把「配置 / 路径 / dict / None」统一成 NapCatBotConfig。"""
    if isinstance(config, NapCatBotConfig):
        return config
    if isinstance(config, (str, Path)):
        return NapCatBotConfig(config)
    if isinstance(config, dict):
        return NapCatBotConfig.from_dict(config, mode=mode)
    if config is None:
        return NapCatBotConfig()
    raise TypeError(f"不支持的连接配置类型：{type(config).__name__}")


def _decode(raw):
    """把 WS 收到的原始数据解成 dict；不是 JSON 对象就返回 None。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def _close_quietly(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


async def _swallow(task) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# 旧名字：反向 WS 的实现已经并入本模块，保留别名让老代码 / 老 import 继续能用
NapCatReverseWS = NapCatConnection


__all__ = [
    "MODE_FORWARD",
    "MODE_REVERSE",
    "NapCatBotConfig",
    "NapCatConnection",
    "NapCatReverseWS",
    "gettoken",
    "normalize_mode",
]
