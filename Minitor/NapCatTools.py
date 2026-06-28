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

import jieba

# 跨平台图片目录：NapCattools.py 在 Minitor/ 下，imgs 在项目根目录
_IMGS_DIR = Path(__file__).resolve().parent.parent / "imgs"


DEBUG = True

class MessageProcessor:
    def __init__(self, config: NapCatBotConfig):
        self.extraconfig = {
            "tokenizer":False,
            'recent':True
        }
        self.config = config
        self.nc = nc(config)
        self.user_id: str = None
        self._agent = None
        self.sllm = None  # 后台线程惰性初始化
        self._agent_ready = threading.Event()
        self._connect_lock = asyncio.Lock()  # 防止并发重复建连
        self._heartbeat_task: asyncio.Task = None
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

    async def _heartbeat_loop(self):
        """后台心跳协程：每 10~20 分钟（均匀分布）调用 get_login_info 保活"""
        while True:
            interval = random.uniform(10 * 60, 20 * 60)  # 10~20 分钟
            await asyncio.sleep(interval)
            try:
                await self._ensure_connected()
                result = await self.nc.get_login_info()
                if DEBUG:
                    print(f"[心跳] get_login_info 成功: {result.get('data', {}).get('nickname', 'unknown')}")
            except Exception as e:
                print(f"[心跳] 异常: {e}")

    def start_heartbeat(self):
        """启动心跳后台任务（需在事件循环中调用）"""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            print("心跳任务已启动（间隔 10~20 分钟）")

    @property
    def agent(self):
        """返回 QQBotAgent，如果后台加载尚未完成则等待"""
        self._agent_ready.wait()
        return self._agent

    @staticmethod
    def tokenizer(text: str) -> str:
        """中文分词：基于 jieba 分词库，返回空格分隔的字符串。

        示例:
            tokenizer("我是独角兽。")
            → '我 是 独角兽 。'
        """
        return " ".join(jieba.cut(text))

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
        """确保 API WebSocket 已连接且 reader 存活（带锁，自动重连）"""
        async with self._connect_lock:
            # 检查 reader 是否还活着（连接可能已异常断开）
            reader_dead = (
                self.nc._reader_task is not None and self.nc._reader_task.done()
            )
            if reader_dead:
                # 等待 reader 的 finally 清理完成
                try:
                    await self.nc._reader_task
                except Exception:
                    pass
                self.nc._reader_task = None
            if self.nc._ws_conn is None or self.nc._reader_task is None:
                await self.nc.connect()

    async def send_message(self, event, message):
        """根据事件类型发送消息"""
        if isinstance(message, list):
            n = len(message)
            index = random.randint(0,n-1)
            message = message[index]

        if self.extraconfig['tokenizer']:
            message = self.tokenizer(message)

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
            await solution(event=event, **args)
            return True
        return False

    async def setuserid(self):
                # 确保已获取自己的 QQ 号
        if self.user_id is None:
            await self._ensure_connected()
            self.user_id = await self.nc.get_user_id()
        # 启动心跳保活任务
        self.start_heartbeat()
        return
    



    
    async def set_tokenizer(self,event):
        raw_message = event['raw_message']
        match = re.search(r'True',raw_message)
        if match:
            self.extraconfig['tokenizer'] = True
            return True
        match = re.search(r'False',raw_message)
        if match:    
            self.extraconfig['tokenizer'] = False
            return True
        return False
        

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
    
    async def auto_forward(self,event,groupid_list,target_groupid) -> bool:
        DEBUG = False
        if event["message_type"] != "group" :
            return False
        if event["group_id"] not in groupid_list:
            if DEBUG:
                print("GROUP_ID NOT CORRECT")
            return False
        raw_msg = event['raw_message']
        match = re.search(r'\[CQ:forward,id=(\d+)\]',raw_msg)
        if not match:
            if DEBUG:
                print("Match Failed!")
            return False
        # node_id = match.group(1)
        for group_id in target_groupid:
            if group_id == event['group_id']:
                continue
            await self._ensure_connected()
            await self.nc.forward_group_single_msg(group_id=group_id,message_id=event['message_id'])
        if DEBUG:
            print("Succeed Forward!")
        return True
    

    async def get_long_history(self,group_id,message_id,remaining:int):
        if remaining <= 0:
            return []

        part = []
        DEBUG = True

        last_msg_id = message_id

        while remaining > 0:
            print('reamaining = ',remaining)
            await self._ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
            group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history['data']['messages'][0]["message_seq"]
            print(icl_history['data']['messages'])
            print('last_msg_id',last_msg_id)
            icl_history['data']['messages'].pop()
            part = icl_history['data']['messages'] + part
            remaining-=1

        return part


    async def get_history_msg(self, event) -> str:
        await self._ensure_connected()
        icl_history = await self.nc.get_group_msg_history(
            group_id=event['group_id']
        )
        parts = []
        for ctx in icl_history['data']['messages']:
            sender = ctx.get('sender', {})
            card = sender.get('card', '') or sender.get('nickname', '')
            parts.append(
                f"[sender:{card} sender_id:{sender.get('user_id', '')} "
                f"messages: {ctx.get('raw_message', '')}]"
            )
        return "\n".join(parts)

    async def get_long_history_test(self,event):
        print('enter History Debug')
        res = await self.get_long_history(group_id=event['group_id'],message_id=event['message_id'],remaining=10)
        print(res)
        print('end')

    async def get_history_msg_test(self,event):
        await self._ensure_connected()
        #获取最近20条
        icl_history = await self.nc.get_group_msg_history(
            group_id=event['group_id']
        )

        count = 0

        for ctx in icl_history['data']['messages']:
            count +=1
            if count <= 10:
                continue
            msg_seq = ctx['message_seq']
            print('###\n'*3)
            await self._ensure_connected()
            temp = await self.nc.get_group_msg_history(
            group_id=event['group_id'],message_seq=msg_seq
            )
            print(str(temp['data']['messages']))
            print('###\n'*3)
            




    async def process_message(self, event):
        message_id = event["message_id"]
        user_id = event["user_id"]
        raw_msg = event["raw_message"]

        if await self.auto_forward(event=event,groupid_list=[1054955587,1079845768],target_groupid=[1079845768,1054955587]):
            return
        # 关键词快速匹配（不走 AI，省 token）— 匹配到任意一个就跳过后续处理
        if (await self.pattern_match(event, r"男娘", self.send_message, {"message": "哪有男娘"})
            or await self.pattern_match(event, r"女装", self.send_message,{"message": ["看看女装","羡慕女装"]})
            or await self.pattern_match(event, r"药娘", self.send_img,{"img_addr": str(_IMGS_DIR / "img01.jpg")})
            or await self.pattern_match(event, r"校园网", self.Check_Campus_NetWoerk)):
            return
        
        if await self.pattern_match(event,r'/historydebug',self.get_long_history_test,{}):
            return
        

        if await self.pattern_match(event,r'/set_tokenizer',self.set_tokenizer,{}):
            return
        if await self.pattern_match(event,r'/get_history_msg_test',self.get_history_msg_test,{}):
            return
        

        # 被 @ 时 → 交给 AI Agent 处理
        if await self.is_AT(raw_msg, self.user_id):
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
            iclhistory = None
            if self.extraconfig['recent']:
                await self._ensure_connected()
                iclhistory = await self.get_history_msg(event)
                print(iclhistory)
            context = (
                f"当前是群聊，群号 {event['group_id']}，"
                f"发消息的用户 QQ 号是 {user_id}，消息 ID 是 {message_id}"
                if event["message_type"] == "group"
                else f"当前是私聊，用户 QQ 号是 {user_id}",
                f'历史消息{str(iclhistory)}' if iclhistory else ''

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
        