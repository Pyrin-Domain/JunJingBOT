"""
反向 WebSocket 连接管理器。
NapCat 作为客户端主动连接到此服务器，同一连接同时处理事件推送和 API 调用。
"""
import asyncio
import json
import uuid
import websockets
from typing import Optional, Callable, Any


class NapCatReverseWS:
    """反向 WebSocket 连接管理器

    启动 WS 服务器等待 NapCat 连接，连接建立后：
    - NapCat 推送的事件通过 ``event_handler`` 分发
    - ``call_api()`` 发送 API 请求并通过 echo 等待响应
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, token: str = ""):
        self._host = host
        self._port = port
        self._token = token

        # NapCat 的连接（同一连接处理事件 + API）
        self._ws: Optional[websockets.WebSocketServerProtocol] = None
        self._server: Optional[websockets.WebSocketServer] = None
        self._reader_task: Optional[asyncio.Task] = None

        # 事件分发
        self._event_handler: Optional[Callable[[dict], Any]] = None

        # API 调用（echo -> Future）
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()

        # 连接状态
        self._connected = asyncio.Event()

    # ── 服务器生命周期 ──────────────────────────────────────────

    async def start(self):
        """启动反向 WS 服务器"""
        self._server = await websockets.serve(
            self._on_connect,
            self._host,
            self._port,
            # ping_interval 可减少意外断连
            ping_interval=30,
            ping_timeout=10,
        )
        print(f"反向 WebSocket 服务器启动在 ws://{self._host}:{self._port}")
        print("等待 NapCat 连接…")

    async def stop(self):
        """关闭服务器"""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._connected.clear()

    # ── 连接处理 ────────────────────────────────────────────────

    async def _on_connect(self, websocket: websockets.WebSocketServerProtocol):
        """NapCat 连接上来的回调"""
        # 可选：校验 token（从 query / header 中）
        # token = dict(websocket.request.query).get("access_token", [None])[0]
        # if token != self._token:
        #     await websocket.close(4001, "token 不匹配")
        #     return

        # 替换旧连接（NapCat 重连时）
        old_ws = self._ws
        if old_ws is not None:
            await old_ws.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        self._ws = websocket
        self._connected.set()
        print("NapCat 已连接（反向 WebSocket）")

        try:
            # 读取 NapCat 发来的所有消息
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._dispatch(data)
        except websockets.ConnectionClosed:
            print("NapCat 连接已关闭")
        except Exception as e:
            print(f"反向 WS 连接异常: {e}")
        finally:
            self._ws = None
            self._connected.clear()
            print("NapCat 连接已清理")

    # ── 消息分发 ────────────────────────────────────────────────

    def _dispatch(self, data: dict):
        """将收到的消息分发给等待的 API 回调或事件处理器"""
        echo = data.get("echo")
        if echo is not None and echo in self._pending:
            future = self._pending.pop(echo)
            if not future.done():
                future.set_result(data)
        elif self._event_handler is not None:
            # 事件不阻塞 reader
            asyncio.create_task(self._event_handler(data))

    # ── 公开 API ────────────────────────────────────────────────

    def set_event_handler(self, handler: Callable[[dict], Any]):
        """注册事件处理器（收到的事件会传给此回调）"""
        self._event_handler = handler

    async def wait_for_connect(self):
        """阻塞直到 NapCat 连接上来"""
        await self._connected.wait()

    async def call_api(self, action: str, params: dict | None = None) -> dict:
        """通过反向 WS 连接发送 API 请求并等待 NapCat 的响应

        Raises:
            RuntimeError: NapCat 尚未连接
            asyncio.TimeoutError: 30 秒内未收到响应
        """
        if self._ws is None:
            raise RuntimeError("NapCat 未连接，无法调用 API")

        if params is None:
            params = {}

        echo_id = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo_id}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[echo_id] = future

        async with self._send_lock:
            await self._ws.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(echo_id, None)
            raise
