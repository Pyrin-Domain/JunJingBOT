import json
import asyncio
import uuid
import websockets
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from Websockets import NapCatBotConfig

class NapCatAPIInterface:
    def __init__(self, config: "NapCatBotConfig"):
        from Websockets import NapCatBotConfig
        self.config = config
        self._ws_conn: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_url = self.config.WS_URL
        # 并发安全：后台reader + 分发机制
        self._reader_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}  # echo -> Future

    async def connect(self):
        """建立API专属WebSocket长连接，并启动后台reader"""
        if self._ws_conn is not None:
            return
        self._ws_conn = await websockets.connect(self._ws_url)
        self._reader_task = asyncio.create_task(self._reader())
        print("API专用WebSocket连接成功")

    async def close(self):
        """关闭API WS连接"""
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
        """后台协程：持续读取WS消息，按 echo 分发给等待的 _call_api"""
        try:
            while True:
                resp_raw = await self._ws_conn.recv()
                resp = json.loads(resp_raw)
                echo = resp.get("echo")
                if echo is not None and echo in self._pending:
                    future = self._pending.pop(echo)
                    if not future.done():
                        future.set_result(resp)
                # 没有 echo 的消息（如心跳）直接丢弃
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"API Reader 异常: {e}")

    async def _call_api(self, action: str, params: dict) -> dict:
        """通用API调用底层方法（并发安全）"""
        if self._ws_conn is None:
            raise RuntimeError("请先调用 connect() 建立WS连接")

        echo_id = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo_id}

        # 创建 Future 并注册到 _pending，再用锁保护 send 避免消息交错
        future = asyncio.get_running_loop().create_future()
        self._pending[echo_id] = future

        async with self._send_lock:
            await self._ws_conn.send(json.dumps(payload))

        try:
            return await future
        finally:
            # 清理（超时等情况）
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
    