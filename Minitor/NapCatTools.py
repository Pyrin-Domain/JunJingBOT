import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import asyncio
import threading
from pathlib import Path
from Websockets import NapCatBotConfig
import re
from NapCatAPI import NapCatAPIInterface as nc

# 跨平台图片目录：NapCattools.py 在 Minitor/ 下，imgs 在项目根目录
_IMGS_DIR = Path(__file__).resolve().parent.parent / "imgs"


class MessageProcessor:
    def __init__(self, config: NapCatBotConfig):
        self.config = config
        self.nc = nc(config)
        self.user_id: str = None
        self._agent = None
        self.sllm = None  # 后台线程惰性初始化
        self._agent_ready = threading.Event()
        self._connect_lock = asyncio.Lock()  # 防止并发重复建连
        # 后台线程预加载 AI Agent + SLLM，不阻塞主线程
        self._agent_thread = threading.Thread(
            target=self._init_agent_bg, daemon=True
        )
        self._agent_thread.start()

    def _init_agent_bg(self):
        """在后台线程中导入并初始化 QQBotAgent 和 SimpleLLM"""
        print("后台预加载 AI Agent …")
        from Agent import QQBotAgent
        self._agent = QQBotAgent(napcat_api=self.nc)
        from Agent.SimpleLLM import SimpleChatModule as SLLM
        self.sllm = SLLM()
        self._agent_ready.set()
        print("AI Agent 后台加载完成")

    @property
    def agent(self):
        """返回 QQBotAgent，如果后台加载尚未完成则等待"""
        self._agent_ready.wait()
        return self._agent

    async def is_AT(self, raw_message: str, user_id: str) -> bool:
        """检查消息是否包含@指定用户"""
        match = re.search(r"\[CQ:at,qq=(\d+)\]", raw_message)
        return match is not None and match.group(1) == user_id

    async def get_message_history(
        self, message_id: int
    ) -> dict["context":str, "index":int]:
        """获取指定消息的历史记录"""
        await self._ensure_connected()
        event = await self.nc.get_message(message_id)
        raw_msg = event["data"]["raw_message"]
        match = re.match(r"\[CQ:reply,id=(\d+)\]", raw_msg)
        if match:
            reply_msg_id = int(match.group(1))
            clean_msg = re.sub(r"^\[CQ:reply,id=\d+\]", "", raw_msg).strip()
            temp = await self.get_message_history(reply_msg_id)
            return {
                "context": temp["context"] + "\n" + str(temp["index"] + 1) + clean_msg,
                "index": temp["index"] + 1,
            }
        else:
            return {"context": "1" + raw_msg, "index": 1}

    async def _ensure_connected(self):
        """确保 API WebSocket 已连接（带锁，防止并发重复建连）"""
        async with self._connect_lock:
            if self.nc._ws_conn is None:
                await self.nc.connect()

    async def send_message(self, event, message):
        """根据事件类型发送消息"""
        if isinstance(message, list):
            n = len(message)
            index = random.randint(0,n-1)
            message = message[index]

        await self._ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_message(
                group_id=event["group_id"], message=message
            )
        elif event["message_type"] == "private":
            await self.nc.send_private_message(user_id=event["user_id"], message=message)

    async def send_img(self,event,img_addr):
        """根据事件类型发送消息"""
        if isinstance(img_addr, list):
            n = len(img_addr)
            index = random.randint(0,n-1)
            img_addr = img_addr[index]

        await self._ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_img(
                group_id=event["group_id"], img_addr=img_addr
            )
        elif event["message_type"] == "private":
            await self.nc.send_private_img(user_id=event["user_id"], img_addr=img_addr)

    async def history_solution(self,event):
        message_id = event['message_id']
        history = await self.get_message_history(message_id)
        print(history)
        await self.send_message(event,history['context'])

    async def pattern_match(self, event, pattern,solution,param=None):
        message_id = event["message_id"]
        user_id = event["user_id"]
        raw_msg = event["raw_message"]

        if param == None:
            args = {}
        else :
            args = param

        if re.search(pattern, raw_msg):
            await solution(event, **args)
            return True
        return False

    async def setuserid(self):
                # 确保已获取自己的 QQ 号
        if self.user_id is None:
            await self._ensure_connected()
            self.user_id = await self.nc.get_user_id()
        return
    
    async def Check_Campus_NetWoerk(self,event):
        if self.sllm == None:
            print("SLLM for Check hasn't benn deployed")
            return
        raw_msg =event['raw_message']
        message = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg).strip()
        print(f"sendMessage:{raw_msg}")
        isVailde = await self.sllm.ask(message)
        if isVailde:
            print('Now Peppare to send img')
            await self.send_img(event, str(_IMGS_DIR / "img02.jpg"))
        return
    

    async def process_message(self, event):
        message_id = event["message_id"]
        user_id = event["user_id"]
        raw_msg = event["raw_message"]

        # 关键词快速匹配（不走 AI，省 token）— 匹配到任意一个就跳过后续处理
        if (await self.pattern_match(event, r"男娘", self.send_message, {"message": "哪有男娘"})
            or await self.pattern_match(event, r"女装", self.send_message,{"message": ["看看女装","羡慕女装"]})
            or await self.pattern_match(event, r"药娘", self.send_img,{"img_addr": str(_IMGS_DIR / "img01.jpg")})
            or await self.pattern_match(event, r"校园网", self.Check_Campus_NetWoerk)):
            return



        await self._ensure_connected()

        # 被 @ 时 → 交给 AI Agent 处理
        if await self.is_AT(raw_msg, self.user_id) or event["message_type"] != "group":
            # 去除 CQ 码，提取纯文本
            clean_text = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg).strip()
            
            match = re.match(r'^历史',clean_text)
            if match :
                print('SkipLLM:Clean_text:',clean_text)
                await self.history_solution(event)
                return

            # 构建上下文
            thread_id = (
                f"group_{event['group_id']}"
                if event["message_type"] == "group"
                else f"private_{user_id}"
            )
            context = (
                f"当前是群聊，群号 {event['group_id']}，"
                f"发消息的用户 QQ 号是 {user_id}，消息 ID 是 {message_id}"
                if event["message_type"] == "group"
                else f"当前是私聊，用户 QQ 号是 {user_id}"
            )
            isDom = '{isDom:true}' if user_id==1013098110 else '{isDom:false}'
            print(str(user_id)+isDom)
            print(f"[AI] 用户 {user_id} 提问: {clean_text}")
            reply = await self.agent.chat(
                user_message=isDom+clean_text,
                thread_id=thread_id,
                extra_context=context,
            )
            print(f"[AI] 回复: {reply}")

            # Agent 如果调用了 send_xxx 工具，消息已发出；
            # 如果 Agent 只是返回文本，我们需要手动发送。
            # 判断：如果 reply 不为空且不是工具返回的"已成功发送"类消息
            if reply and "已成功发送" not in reply:
                await self.send_message(event, reply)
        