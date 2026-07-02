import json
import asyncio
import uuid
import websockets
from typing import Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from Websockets import NapCatBotConfig
    from reverse_ws import NapCatReverseWS

class NapCatAPIInterface:
    """NapCat API 接口

    支持两种模式：
    - **正向 WS**（旧）：传入 ``NapCatBotConfig``，自建连接
    - **反向 WS**（新）：传入 ``NapCatReverseWS``，复用共享连接
    """

    def __init__(self, connection: Union["NapCatBotConfig", "NapCatReverseWS"]):
        if hasattr(connection, "_pending"):
            # ── 反向 WS 模式 ──
            from reverse_ws import NapCatReverseWS
            self._rws: NapCatReverseWS = connection
            self._ws_conn = None
            self._reader_task = None
            self._send_lock = None
            self._pending = None
        else:
            # ── 正向 WS 模式（向后兼容） ──
            from Websockets import NapCatBotConfig
            self._rws = None
            self.config: NapCatBotConfig = connection
            self._ws_conn: Optional[websockets.WebSocketClientProtocol] = None
            self._ws_url = self.config.WS_URL
            self._reader_task: Optional[asyncio.Task] = None
            self._send_lock = asyncio.Lock()
            self._pending: dict[str, asyncio.Future] = {}

    async def connect(self):
        """建立连接

        反向 WS 模式：等待 NapCat 连接上来；
        正向 WS 模式：主动连接 NapCat（旧行为）。
        """
        if self._rws is not None:
            await self._rws.wait_for_connect()
            return
        # 正向 WS 逻辑（旧）
        if self._ws_conn is not None:
            return
        self._ws_conn = await websockets.connect(self._ws_url)
        self._reader_task = asyncio.create_task(self._reader())
        print("API专用WebSocket连接成功")

    async def close(self):
        """关闭连接"""
        if self._rws is not None:
            # 反向 WS：不关闭共享连接，由主控方管理
            return
        # 正向 WS 清理
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws_conn is not None:
            await self._ws_conn.close()
            self._ws_conn = None

    async def _reader(self):
        """正向 WS 后台 reader（旧，仅正向模式使用）"""
        try:
            while True:
                resp_raw = await self._ws_conn.recv()
                resp = json.loads(resp_raw)
                echo = resp.get("echo")
                if echo is not None and echo in self._pending:
                    future = self._pending.pop(echo)
                    if not future.done():
                        future.set_result(resp)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"API Reader 异常: {e}")

    async def _call_api(self, action: str, params: dict) -> dict:
        """通用 API 调用

        反向 WS 模式 -> 委托给 ``NapCatReverseWS.call_api()``；
        正向 WS 模式 -> 旧逻辑（自建连接的 echo 机制）。
        """
        if self._rws is not None:
            return await self._rws.call_api(action, params)

        # ── 正向 WS 逻辑（旧） ──
        if self._ws_conn is None:
            raise RuntimeError("请先调用 connect() 建立WS连接")

        echo_id = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo_id}

        future = asyncio.get_running_loop().create_future()
        self._pending[echo_id] = future

        async with self._send_lock:
            await self._ws_conn.send(json.dumps(payload))

        try:
            return await future
        finally:
            self._pending.pop(echo_id, None)

    async def send_group_message(self, group_id: int, message: str) -> dict:
        """发送群消息"""
        return await self._call_api(
            action="send_group_msg", params={"group_id": group_id, "message": message}
        )

    async def send_private_message(self, user_id: int, message: str) -> dict:
        """发送私聊消息"""
        return await self._call_api(
            action="send_private_msg", params={"user_id": user_id, "message": message}
        )

    async def get_message(self, message_id: int) -> dict:
        """获取消息详情"""
        return await self._call_api(action="get_msg", params={"message_id": message_id})

    async def get_user_id(self) -> str:
        temp = await self._call_api(action="get_login_info", params={})
        user_id = temp["data"]["user_id"]
        return str(user_id)

    async def send_group_img(self, group_id: int, img_addr: str, summary: str = "") -> dict:
        """发送群消息"""
        return await self._call_api(
            action="send_group_msg", params={"group_id": group_id, "message": [{"type":"image","data":{"file":img_addr,"summary":summary}}]}
        )
    async def send_private_img(self, user_id: int, img_addr: str, summary: str = "") -> dict:
        """发送私聊消息"""
        return await self._call_api(
            action="send_private_msg", params={"user_id": user_id, "message": [{"type":"image","data":{"file":img_addr,"summary":summary}}]}
        )
    async def send_group_forward_msg(self,group_id:int,node_id):
        """转发群聊消息"""
        return await self._call_api(
            action="send_group_forward_msg", params={"group_id": group_id, "message": [{"type":"node","data":{"id":node_id}}]}
        )
    
    async def forward_group_single_msg(self,group_id:int,message_id:int):
        """转发单条消息"""
        return await self._call_api(
            action = 'forward_group_single_msg' , params={'group_id':group_id,'message_id':message_id}
        )
    
    async def forward_friend_single_msg(self,user_id:int,message_id:int):
        """转发单条消息"""
        return await self._call_api(
            action = 'forward_friend_single_msg' , params={'user_id':user_id,'message_id':message_id}
        )
    
    async def get_forward_msg(self, params: dict):
        """获取转发消息"""
        return await self._call_api(
            action="get_forward_msg", params={**params}
        )
    
    async def get_login_info(self):
        """获取登录信息防止被踢下线"""
        return await self._call_api(
            action="get_login_info",params={}
        )
    
    async def get_group_msg_history(self,group_id,message_seq=None,reverse_order=False,count=20):
        """获取群聊历史聊天记录,reverse_order=True 向前拉取，reverse_order=False 向后拉取"""
        params = {'group_id':group_id,'reverse_order':reverse_order,'count':count}
        if message_seq :
            params['message_seq'] = message_seq
        return await self._call_api(
            action='get_group_msg_history',params=params
        )
    
    async def get_friend_msg_history(self,user_id,message_seq=None,reverse_order=False,count=20):
        """获取好友历史聊天记录,reverse_order=True 向前拉取，reverse_order=False 向后拉取"""
        params = {'user_id':user_id,'reverse_order':reverse_order,'count':count}
        if message_seq :
            params['message_seq'] = message_seq
        return await self._call_api(
            action='get_friend_msg_history',params=params
        )