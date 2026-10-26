"""OneBot WebSocket 连接（正向 / 反向统一实现）。

支持 NapCat / SnowLuma 等 OneBot v11 后端，协议层行为完全一致，只有日志里的
后端名称和默认值来自 :mod:`config.backends`。

两种模式对外接口完全一样，上层不关心是哪种：

    conn = OneBotConnection(settings)       # ConnectionSettings（或 dict / 路径）
    await conn.start()
    conn.set_event_handler(on_event)        # 事件回调（同步 / 异步都行）
    await conn.wait_for_connect()
    resp = await conn.call_api("get_login_info")

行为与旧 ``Minitor/Websockets.py`` 逐条对应：

- 反向 ``ws_re``：``websockets.serve`` 起服务器；新客户端连入时作废旧 pending 并关掉旧连接
- 正向 ``ws``：后台连接 + 断线 5 秒重连
- token 从 query ``access_token`` 或 ``Authorization`` 头读取；默认不严格校验
- ``call_api`` 用 ``echo`` 配对 future，默认 30 秒超时
- ping 30 秒 / timeout 10 秒
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.exceptions import ConnectionClosed

from .config import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_RETRY_SECONDS,
    PING_INTERVAL,
    PING_TIMEOUT,
    ConnectionSettings,
    coerce_connection_settings,
)


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


class OneBotConnection:
    """正向 / 反向通用的 OneBot 连接。

    一条连接同时负责「收事件」和「发 API」。正向模式下发 API 用的是
    ``sender_config.json`` 那条独立连接（另建一个实例，两条互不干扰）。
    """

    def __init__(
        self,
        config: ConnectionSettings | dict | str | Path | None = None,
        *,
        role: str = "",
        mode: str | None = None,
    ):
        self.config: ConnectionSettings = coerce_connection_settings(config, mode=mode)
        self.role = role or ""
        self._label = f"（{self.role}）" if self.role else ""

        # 连接对象
        self._ws = None                    # 当前连接（两种模式共用）
        self._server = None                # 反向：WS 服务器
        self._connect_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None   # 兼容旧字段（本实现不用）
        self._started = False
        self._closing = False
        self._bg_tasks: set[asyncio.Task] = set()

        # 事件分发 / API 调用
        self._event_handler: Callable[[dict], Any] | None = None
        self._connect_listeners: list[Callable[[], Any]] = []
        self._disconnect_listeners: list[Callable[[], Any]] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()

    # ── 属性 ──
    @property
    def backend(self) -> str:
        return self.config.backend

    @property
    def backend_label(self) -> str:
        return self.config.backend_label

    @property
    def history_anchor(self) -> str:
        return self.config.history_anchor

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
    async def start(self) -> "OneBotConnection":
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
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT,
            )
            print(
                f"[WS] 反向服务器已启动 ws://{host}:{port}{self._label}，"
                f"等待 {self.backend_label} 连接…"
            )
        else:
            self._connect_task = asyncio.create_task(self._connect_loop())
            print(
                f"[WS] 正向模式：连接 {self.config.endpoint}{self._label}"
                f"（{self.backend_label}）…"
            )
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

    # ── 反向：后端连上来 ──
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
            self._fail_pending(f"{self.backend_label} 重新连接，旧请求已作废")
            self._spawn(_close_quietly(old))

        self._ws = ws
        self._connected.set()
        print(
            f"[WS] {self.backend_label} 已连接（反向 ws_re）"
            f"{self._label} {self._peer(ws)}"
        )
        self._notify_connected()
        try:
            await self._read_loop(ws)
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()
                self._fail_pending(f"{self.backend_label} 连接已断开")
                self._notify_disconnected()
            print(f"[WS] {self.backend_label} 连接已断开{self._label}")

    # ── 正向：主动连后端（断线自动重连） ──
    async def _connect_loop(self) -> None:
        url = self.config.ws_url
        while not self._closing:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                    proxy=None,          # 本机连接不走系统代理
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    print(
                        f"[WS] 已连接 {self.backend_label}（正向 ws）"
                        f"{self._label} {self.config.endpoint}"
                    )
                    self._notify_connected()
                    try:
                        await self._read_loop(ws)
                    finally:
                        if self._ws is ws:
                            self._ws = None
                        self._connected.clear()
                        self._fail_pending(f"{self.backend_label} 连接已断开")
                        self._notify_disconnected()
                        print(f"[WS] {self.backend_label} 连接已断开{self._label}")
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
        except ConnectionClosed:
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
        print(
            "[WS] 警告：连上来的 access_token 与配置不一致"
            "（ws_reverse_strict_token=false，先放行）"
        )
        return True

    @staticmethod
    def _peer(ws) -> str:
        addr = getattr(ws, "remote_address", None)
        if isinstance(addr, tuple) and len(addr) >= 2:
            return f"{addr[0]}:{addr[1]}"
        return str(addr or "?")

    # ── 对外 API ──
    def set_event_handler(self, handler: Callable[[dict], Any]) -> "OneBotConnection":
        """注册事件处理器（同步 / 异步都行）。"""
        self._event_handler = handler
        return self

    def add_connect_listener(self, listener: Callable[[], Any]) -> "OneBotConnection":
        """注册连接建立回调（每次连上 / 重连都会调用；同步 / 异步都行）。"""
        if listener not in self._connect_listeners:
            self._connect_listeners.append(listener)
        return self

    def add_disconnect_listener(
        self, listener: Callable[[], Any]
    ) -> "OneBotConnection":
        """注册连接断开回调（每次断开都会调用；同步 / 异步都行）。"""
        if listener not in self._disconnect_listeners:
            self._disconnect_listeners.append(listener)
        return self

    async def wait_for_connect(self, timeout: float | None = None) -> bool:
        """等后端连上来；timeout 为 None 时一直等。返回是否等到了。"""
        if timeout is None:
            await self._connected.wait()
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def ensure_connected(self, timeout: float | None = None) -> "OneBotConnection":
        """没启动就先启动，没连上就等一下（面板 / 心跳之类的懒加载用）。"""
        if not self._started:
            await self.start()
        if not self.is_connected and not await self.wait_for_connect(timeout):
            raise TimeoutError(
                f"等待 {self.backend_label} 连接超时（{self.describe()}）"
            )
        return self

    async def call_api(
        self,
        action: str,
        params: dict | None = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> dict:
        """发一条 API 请求并等后端的响应（echo 配对）。

        Raises:
            RuntimeError: 还没连上后端
            TimeoutError: timeout 秒内没收到响应（默认 30 秒）
        """
        if self._ws is None:
            raise RuntimeError(
                f"{self.backend_label} 未连接"
                f"（{self.config.mode_label}{self._label}），无法调用 {action}；"
                f"请确认 {self.config.path} 里的 backend / type / 端口，"
                f"以及 {self.backend_label} 端的 WebSocket 配置"
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
    def _notify_connected(self) -> None:
        for listener in list(self._connect_listeners):
            self._spawn(self._safe_state_listener("连接", listener))

    def _notify_disconnected(self) -> None:
        for listener in list(self._disconnect_listeners):
            self._spawn(self._safe_state_listener("断开", listener))

    async def _safe_state_listener(self, kind: str, listener) -> None:
        try:
            result = listener()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[WS] {kind}回调异常{self._label}：{exc!r}")

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


# 旧名字：反向 WS 的实现已经并入本模块，保留别名让老代码继续能用
NapCatConnection = OneBotConnection
NapCatReverseWS = OneBotConnection


__all__ = [
    "OneBotConnection",
    "NapCatConnection",
    "NapCatReverseWS",
]
