import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import asyncio
import threading
import inspect
from pathlib import Path
from Websockets import NapCatBotConfig
import re
from NapCatAPI import NapCatAPIInterface as nc
from extension import Extension
import jieba
import json
from copy import deepcopy

# ---- 从 paths_config.json 读取图片目录（适配 WSL / 跨平台） ----
def _load_imgs_dir() -> str:
    """加载图片目录配置，优先读 paths_config.json，fallback 到本地 imgs/"""
    config_path = Path(__file__).resolve().parent.parent / "paths_config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        imgs_path = cfg.get("imgs_dir")
        if imgs_path:
            return imgs_path  # 直接用原始字符串（WSL 路径如 /mnt/d/...）
    except Exception:
        pass
    # Fallback：项目根目录下的 imgs/
    return str(Path(__file__).resolve().parent.parent / "imgs")


_IMGS_DIR = _load_imgs_dir()
# ----


# DEBUG = True


class MessageProcessor:
    def __init__(self, config: NapCatBotConfig):
        self.extraconfig = {"tokenizer": False, "recent": True}
        self.ifs = image_forward_service(self)
        self.config = config
        self.nc = nc(config)
        self.extension = Extension()
        self.user_id: str = None
        self._agent = None
        # self.sllm = None  # 后台线程惰性初始化
        self.campus_assistant = None  # 校园网助手，后台线程惰性初始化
        self._agent_ready = threading.Event()
        self._connect_lock = asyncio.Lock()  # 防止并发重复建连
        self._heartbeat_task: asyncio.Task = None
        self._checkpoint_save_task: asyncio.Task = None
        self.check_point: dict = {}
        # 后台线程预加载 AI Agent + SLLM，不阻塞主线程
        self._agent_thread = threading.Thread(target=self._init_agent_bg, daemon=True)
        self._agent_thread.start()

    async def process_message(self, event):
        DEBUG = False
        # 每条消息进来立即记录断点（崩了也知道处理到哪条了）
        self.check_point[str(event.get("group_id", event["user_id"]))] = event

        message_id = event["message_id"]
        user_id = event["user_id"]
        # raw_msg = event["raw_message"]

        if await self.auto_forward(
            event=event,
            groupid_list=[
                1054955587,
                1079845768,
                320955551,
                569117421,
                662802665,
                973208344,
            ],
            target_groupid=[1079845768, 1054955587, 320955551, 973208344],
        ):
            return
        # 关键词快速匹配（不走 AI，省 token）
        pattern_config = [
            {
                "regex": r"(男娘|南梁|nn)",
                "solution": self.send_message,
                "params": {"message": "哪有男娘"},
            },
            {
                "regex": r"女装",
                "solution": self.send_message,
                "params": {"message": ["看看女装", "羡慕女装"]},
            },
            {
                "regex": r"药娘",
                "solution": self.send_img,
                "params": {"img_addr": f"{_IMGS_DIR}/img01.jpg",'summary':"死人妖"},
            },
            
            {"regex": r"/historydebug", "solution": self.get_long_history_test},
            {"regex": r"/export", "solution": self.export},
            {"regex": r"/set_tokenizer", "solution": self.set_tokenizer},
            {"regex": r"ocr", "solution": self.ocr},
            {"regex": r"/img_fd ", "solution": self.img_forward_create_task},
        ]

        asyncio.create_task(self.pattern_match(event=event, pattern_config=pattern_config))
        
        if await self.pattern_match(event=event, pattern_config=[{"regex": r"校园网", "solution": self.Check_Campus_NetWoerk}]):
            return
        
        # 被 @ 时 → 交给 AI Agent 处理

        ated_user = await self.is_AT(event=event, user_id=[self.user_id, "1013098110"])
        if ated_user:
            # 去除 CQ 码，提取纯文本
            clean_text = self.get_clean_context(event, True)
            match = re.match(r"^历史", clean_text)
            if match:
                DEBUG and print("SkipLLM:Clean_text:", clean_text)
                await self.history_solution(event)
                return
            # 构建上下文
            thread_id = (
                f"group_{event['group_id']}"
                if event["message_type"] == "group"
                else f"private_{user_id}"
            )
            iclhistory = None
            if self.extraconfig["recent"]:
                await self._ensure_connected()
                iclhistory = await self.get_history_msg(event)
                print(iclhistory)
            context = (
                (
                    f"当前是群聊，群号 {event['group_id']}，"
                    f"发消息的用户 QQ 号是 {user_id}，消息 ID 是 {message_id},AT的对象的QQ号是{ated_user},你的QQ号是{self.user_id},主人的QQ号是{'1013098110'}"
                    if event["message_type"] == "group"
                    else f"当前是私聊，用户 QQ 号是 {user_id}"
                ),
                f"历史消息如下:{str(iclhistory)}" if iclhistory else "",
            )
            isDom = "{isDom:true}" if user_id == 1013098110 else "{isDom:false}"
            DEBUG and print(str(user_id) + isDom)
            DEBUG and print(f"[AI] 用户 {user_id} 提问: {clean_text}")
            totoal_msg = await self.generate_structed_message_to_creat_context(
                event=event
            )
            reply = await self.agent.chat(
                user_message=isDom + totoal_msg,
                thread_id=thread_id,
                extra_context=context,
            )
            DEBUG and print(f"[AI] 回复: {reply}")

            # Agent 如果调用了 send_xxx 工具，消息已发出；
            # 如果 Agent 只是返回文本，我们需要手动发送。
            # 判断：如果 reply 不为空且不是工具返回的"已成功发送"类消息
            # 注意: chat() 在异常/空回复时返回 None，天然被 if reply 过滤
            if reply == "__NETWORK__":
                # 主 Agent 判定为校园网问题，转交校园网助手
                await self._route_to_campus_assistant(event, clean_text, iclhistory=iclhistory)
            elif reply == "__SILENT__":
                # 主 Agent 判定为不需要回复，静默处理
                return
            elif reply and "已成功发送" not in reply:
                await self.send_message(event, reply)

    async def ocr_and_send(self, group_id, url):
        ocr_answer = await self.extension.napcat_ocr(url)
        await self._ensure_connected()
        await self.nc.send_group_message(group_id=group_id, message=ocr_answer["text"])

    def get_url(self, msg_event) -> list[str]:
        res = []
        for item in msg_event.get("message", []):
            item_type = item.get("type", "")
            if item_type != "image":
                continue
            url = (item.get("data") or {}).get("url")
            if not url:
                continue
            res.append(url)
        return res

    def is_contain_image(self, msg_event) -> bool:
        for item in msg_event.get("message", []):
            item_type = item.get("type", "")
            if item_type == "image" and not item.get("data", {}).get("summary"):
                return True
        return False

    async def image_forward_processor(self, event, target_id_list, msg_type='group'):
        if not self.is_contain_image(event):
            return
        group_id = event["group_id"] if event.get('message_type') == 'group' else None
        if msg_type == 'group':
            for target_id in target_id_list:
                if target_id == group_id:
                    continue
                await self._ensure_connected()
                await self.nc.forward_group_single_msg(
                group_id=target_id, message_id=event["message_id"]
                )
            return
        if msg_type == 'private':
            for target_id in target_id_list:
                await self._ensure_connected()
                await self.nc.forward_friend_single_msg(
                    user_id=target_id, message_id=event["message_id"]
                )

    async def image_forward(self, group_id, target_id_list, begin_id, end_id,msg_type):
        await self.get_long_history_only_processor(
            group_id=group_id,
            start_id=begin_id,
            end_id=end_id,
            processor=self.image_forward_processor,
            params={"target_id_list": target_id_list,'msg_type':msg_type},
        )

    async def ocr(self, event):
        reply_id = self.is_reply(event)
        if not reply_id:
            return
        await self._ensure_connected()
        res = await self.nc.get_message(message_id=reply_id)
        msg_event = res.get("data", {})

        url_list = self.get_url(msg_event)

        for url in url_list:
            asyncio.create_task(self.ocr_and_send(group_id=event["group_id"], url=url))
        return

    async def img_forward_create_task(self, event):
        clean_msg = self.get_clean_context(event=event)
        rpy = self.is_reply(event)
        match = re.search(r"(-g|-group) (\d+)", clean_msg)
        if match:
            target_id = int(match.group(2))
            await self.ifs.create_task(group_id=event["group_id"],target_id_list=[target_id],msg_type='group')
        else:
            match = re.search(r"(-u|-user) (\d+)", clean_msg)
            if match:
                target_id = int(match.group(2))
                await self.ifs.create_task(group_id=event['group_id'],target_id_list=[target_id],msg_type='private')
            else:
                match = re.search(r"(-a|-auto)", clean_msg)
                if match:
                    target_group_id_list = [1079845768, 1054955587, 320955551, 973208344]
                await self.ifs.create_task(group_id=event['group_id'],target_id_list=target_group_id_list,msg_type='group')


        match_1 = re.search(r"(-begin|-b)", clean_msg)
        if match_1:
            await self.ifs.set_begin_id(
                group_id=event["group_id"], begin_id=rpy or event["message_id"]
            )
        match_2 = re.search(r"(-end|-e)", clean_msg)
        if match_2 and match_1:
            await self.ifs.set_end_id(
                group_id=event["group_id"], end_id=event["message_id"]
            )
        elif match_2:
            await self.ifs.set_end_id(
                group_id=event["group_id"], end_id=rpy or event["message_id"]
            )
        return

    async def is_AT(self, event: dict, user_id: list[str]) -> list | None:
        """检查消息是否包含@指定用户"""
        temp = []
        info = event.get("message", [])
        for item in info:
            item_type = item.get("type", "")
            if item_type != "at":
                continue
            qq_id = str((item.get("data") or {}).get("qq", ""))
            if qq_id in user_id and qq_id not in temp:
                temp.append(qq_id)
        return temp

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

    async def get_group_latest_msg(self,group_id):

        await self._ensure_connected()
        event = await self.nc.get_group_msg_history(group_id=group_id,count=1,reverse_order=True)
        messages = event['data']['messages']
        if not messages:
            print(f'[警告] 群 {group_id} 暂无消息或无法访问')
            return None
        return messages[0]

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
            index = random.randint(0, n - 1)
            message = message[index]

        if self.extraconfig["tokenizer"]:
            message = self.tokenizer(message)

        await self._ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_message(
                group_id=event["group_id"], message=message
            )
            self.check_point[f'{event["group_id"]}'] = await self.get_group_latest_msg(event["group_id"])
        elif event["message_type"] == "private":
            await self.nc.send_private_message(
                user_id=event["user_id"], message=message
            )

    async def send_img(self, event, img_addr,summary="君景发图"):
        """根据事件类型发送消息"""
        if isinstance(img_addr, list):
            n = len(img_addr)
            index = random.randint(0, n - 1)
            img_addr = img_addr[index]

        await self._ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_img(group_id=event["group_id"], img_addr=img_addr, summary=summary)
        elif event["message_type"] == "private":
            await self.nc.send_private_img(user_id=event["user_id"], img_addr=img_addr, summary=summary)

    async def history_solution(self, event):
        message_id = event["message_id"]
        history = await self.get_message_history(message_id)
        print(history)
        await self.send_message(event, history["context"])

    def get_clean_context(self, event, keepAt=False) -> str:
        DEBUG = True
        context = ""
        for item in event.get("message", []):
            msg_type = item.get("type", "")
            if msg_type == "text":
                context += (item.get("data") or {}).get("text", "")
                continue
            if msg_type == "image":
                context += "图片"
                context += (item.get("data") or {}).get("summary", "")
                continue
            if keepAt and msg_type == "at":
                qq_id = (item.get("data") or {}).get("qq", "all")
                context += f"[CQ:at,qq={qq_id}]"
        DEBUG and print(context)
        return context

    async def pattern_match(self, event, pattern_config: list[dict[str, any]]):
        clean_msg = self.get_clean_context(event)
        ret: bool = False
        for pattern in pattern_config:
            solution = pattern.get("solution")
            regex = pattern.get("regex")
            if not solution or not regex:
                continue
            params = pattern.get("params", {})
            if re.search(regex, clean_msg):
                # 自动检测 solution 是否需要 event 参数
                sig = inspect.signature(solution)
                if "event" in sig.parameters:
                    asyncio.create_task(solution(event=event, **params))
                else:
                    asyncio.create_task(solution(**params))
                ret = True
                continue
                # return True
        return ret

    async def read_check_point(self,user_id):
        file_name = str(Path(__file__).resolve().parent.parent / "check_point"/f'{user_id}.json')
        try:
            with open(file_name,mode='r',encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print("FileNotFoundError")
            dir_path = os.path.dirname(file_name)
            os.makedirs(dir_path, exist_ok=True)
            with open(file_name,mode='w',encoding='utf-8') as f:
                json.dump({},f, ensure_ascii=False, indent=4)
                return {}
        except:
            print("error")
        return {}
        
    async def _save_check_point(self):
        """将当前断点写入磁盘"""
        DEBUG = False
        try:
            file_name = str(Path(__file__).resolve().parent.parent / "check_point" / f'{self.user_id}.json')
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
            with open(file_name, 'w', encoding='utf-8') as f:
                json.dump(self.check_point, f, ensure_ascii=False, indent=4)
            if self.check_point:
                DEBUG and print(f"[断点保存] 已写入 {len(self.check_point)} 条断点到 {file_name}")
        except Exception as e:
            print(f"[断点保存] 异常: {e}")

    async def recover_process(self, event,latest_msg_id=None):
        """回放断点消息：从最新消息往前翻，直到找到断点消息为止"""
        DEBUG = True
        group_id = event['group_id']
        rid = event.get('real_id')
        real_seq = event.get('real_seq')
        if not rid and not real_seq:
            return
        remaining = 8  # 最多翻 8 页
        message_seq = latest_msg_id  # None = 从最新开始
        while remaining > 0:
            remaining -= 1
            await self._ensure_connected()
            history_list = await self.nc.get_group_msg_history(
                group_id=group_id, reverse_order=True, count=20, message_seq=message_seq
            )
            messages = history_list['data']['messages']
            if not messages:
                break

            # 记录本次传入的锚点，后面会覆盖 message_seq
            seq_for_this_page = message_seq

            # 翻页锚点 = 本批最旧的消息的 seq（下一次拉更旧的）
            message_seq = messages[0]['message_seq']

            # 非首次调用：API 返回会包含锚点消息（上一批已处理），pop 掉避免重复
            if seq_for_this_page is not None:
                messages.pop()
            if not messages:
                break

            # 从最新→最旧遍历（reverse_order 返回升序 旧→新）
            for msg_event in reversed(messages):
                if msg_event.get('real_id') == rid or msg_event.get('real_seq') == real_seq:
                    print(f'[断点恢复] 找到断点消息 {rid}')
                    latest = await self.get_group_latest_msg(group_id)
                    if latest:
                        self.check_point[f'{group_id}'] = latest
                    return
                # 被踢期间的消息确实没处理过，走完整 process_message
                DEBUG and print(f'[断点恢复] 未找到断点 {rid}，处理消息 {msg_event.get("real_id")}')
                # await self.process_message(msg_event)
        print(f'[断点恢复] 未找到断点 {rid}，可能已被清理')

    async def recover_from_check_point(self):
        """深拷贝断点，创建协程回放（不阻塞主流程）"""
        check_point = deepcopy(self.check_point)
        latest_msg_sqes = {}
        for key, value in check_point.items():
            temp = await self.get_group_latest_msg(group_id=key)
            if temp is None:
                print(f'[断点恢复] 群 {key} 无最新消息，跳过恢复')
                continue
            latest_msg_sqes[key] = {'message_seq':temp["message_seq"], 'real_id':temp.get('real_id'), 'real_seq':temp.get('real_seq')}
            
        for key, value in check_point.items():
            if key not in latest_msg_sqes:
                continue
            print(f'[断点恢复] 创建恢复任务: {key}')
            print(f'[断点恢复] 最新消息 seq: {latest_msg_sqes[key]}')
            if latest_msg_sqes[key]['real_id'] == value.get('real_id') or latest_msg_sqes[key]['real_seq'] == value.get('real_seq'):
                print(f'[断点恢复] 最新消息已是断点消息 {latest_msg_sqes[key]}，无需回放')
                continue    
            asyncio.create_task(self.recover_process(event=value, latest_msg_id=latest_msg_sqes[key]['message_seq']))
        if check_point:
            await self._save_check_point()

    async def _checkpoint_save_loop(self):
        """后台每 10 秒将内存中的断点刷入磁盘"""
        while True:
            await asyncio.sleep(10)
            try:
                await self._save_check_point()
            except Exception as e:
                print(f"[断点保存] 写入失败: {e}")

    async def setuserid(self):
        # 确保已获取自己的 QQ 号
        if self.user_id is None:
            await self._ensure_connected()
            self.user_id = await self.nc.get_user_id()
            self.check_point = await self.read_check_point(self.user_id)
            print(f'[启动] user_id={self.user_id}, 加载断点 {len(self.check_point)} 个')
            # 启动断点定时保存任务
            self._checkpoint_save_task = asyncio.create_task(self._checkpoint_save_loop())
        # 启动心跳保活任务
        self.start_heartbeat()
        return

    async def get_long_history_backward(self, group_id, start_id, end_id):
        DEBUG = False
        remaining = 20  # 最大搜索20次
        part = []
        DEBUG = True
        last_msg_id = start_id
        while remaining > 0:
            DEBUG and print("reamaining = ", remaining)
            await self._ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][-1]["message_seq"]
            icl_history["data"]["messages"].pop()
            part = icl_history["data"]["messages"] + part
            remaining -= 1
            for item in icl_history["data"]["messages"]:
                if item["message_seq"] == end_id:
                    return part
            if item["message_seq"] == end_id:
                return part
        DEBUG and print("NotFind")
        return part

    async def get_long_history_only_processor(
        self, group_id, start_id, end_id, processor, params
    ) -> None:
        remaining = 200  # 最大搜索200次
        DEBUG = True

        last_msg_id = start_id

        while remaining > 0:
            print("reamaining = ", remaining)
            await self._ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][-1]["message_seq"]
            last_msg = icl_history["data"]["messages"].pop()
            remaining -= 1
            for item in icl_history["data"]["messages"]:
                await processor(event=item, **params)
                if item["message_seq"] == int(end_id):
                    DEBUG and print("FIND")
                    return
            if last_msg_id == end_id:
                await processor(event=last_msg, **params)
                DEBUG and print("FIND")
                return
        return

    async def export(self, event):
        DEBUG = True
        message_id = self.is_reply(event)
        if not message_id:
            return False
        clean_msg = self.get_clean_context(event)
        match1 = re.search(r"(-group|-g) (\d+)", clean_msg)
        match2 = re.search(r'(-user|-u) (\d+)', clean_msg)
        if not match1 and not match2:
            DEBUG and print("Find no Target")
            return False
        if match1:
            target_group = int(match1.group(2))
        if match2:
            target_user_id = int(match2.group(2))
        end_id = event["message_id"]
        if match1:
            await self.get_long_history_only_processor(
                group_id=event["group_id"],
                start_id=message_id,
                end_id=end_id,
                processor=self.auto_forward,
                params={
                    "groupid_list": [event["group_id"]],
                    "target_groupid": [target_group],
                },
            )
        if match2:
            print("Prepare to start export user")
            await self.get_long_history_only_processor(
                group_id=event["group_id"],
                start_id=message_id,
                end_id=end_id,
                processor=self.auto_forward,
                params={
                    "groupid_list": [event["group_id"]],
                    "task_userid": [target_user_id],
                },
            )
        return True

    def check_is_miniapp(self, msg_event: dict) -> str | None:
        for seg in msg_event.get("message", []):
            if seg["type"] == "json":
                try:
                    json_data = json.loads(seg["data"]["data"])

                    if json_data.get("app") == "com.tencent.miniapp_01":
                        detail = json_data["meta"]["detail_1"]
                        title = detail["title"]
                        desc = detail["desc"]
                        link = detail["qqdocurl"]
                        return f"[title:{title}][desc:{desc}]"
                except Exception:
                    continue
        return None

    def check_is_multimsg_forward(self, msg_event: dict) -> str | None:
        """
        检测是否为新版合并转发JSON卡片
        :return: resid 字符串（是转发卡片时），否则 None
        """
        for seg in msg_event.get("message", []):
            if seg["type"] == "json":
                try:
                    json_data = json.loads(seg["data"]["data"])
                    if json_data.get("app") == "com.tencent.multimsg":
                        return json_data["meta"]["detail"]["resid"]
                except Exception:
                    continue
        return None

    def check_is_old_forward(self, msg_event: dict) -> bool:
        """确定是不是旧版forward"""
        for seg in msg_event.get("message", []):
            if seg["type"] == "forward":
                return True
        return False

    def is_reply(self, msg_event: dict) -> int | None:
        for seg in msg_event.get("message", []):
            if seg["type"] == "reply":
                return seg["data"]["id"]
        return

    async def generate_structed_message_to_creat_context(
        self, event, isOCR=False
    ) -> str:
        ###感觉可以做一个池来达到Cache命中的效果，因为需要持续去寻找转发消息，尤其是回复比较多的情况
        ###恰好有future可以达到，但是不适合该场景，因为访存和发包的时间肯定天差地别，发包还有网络压力 命中 or get_reply
        ###但是压力不大，效果不显著
        DEBUG = False
        temp = ""
        message_id = event["message_id"]
        DEBUG and print("获取sender")
        sender = event.get("sender") or {}
        user_id = event.get("user_id") or sender.get("user_id", "未知")
        name = sender.get("card") or sender.get("nickname", "未知")
        temp += f"消息发送者{name}，其QQ号为{user_id},\t发送消息[message_id:{message_id}]:"
        DEBUG and print(temp)
        mini_app_info = self.check_is_miniapp(event)
        if mini_app_info:
            return f"[这是一条小程序消息{mini_app_info}]"
        if self.check_is_old_forward(event):
            DEBUG and print("转发模块tradition Begin")

            res = await self.nc.get_forward_msg({"message_id": message_id})

            if res["status"] == "failed":
                return ""

            messages = res["data"]["messages"]
            ret = ""
            for message_event in messages:
                ret += await self.generate_structed_message_to_creat_context(
                    message_event
                )
            temp += f"这是一条转发消息,内容如下[{ret}]\n"
            DEBUG and print("转发模块tradition End")
            return temp
        rid = self.check_is_multimsg_forward(event)
        if rid:
            DEBUG and print("转发模块Json Begin")
            await self._ensure_connected()
            res = await self.nc.get_forward_msg({"message_id": rid})
            if res["status"] == "failed":
                return ""
            messages = res["data"]["messages"]
            ret = ""
            for message_event in messages:
                if (
                    not message_event.get("message")
                    or len(message_event["message"]) == 0
                ):
                    ret += "[这可能是嵌套的聊天记录，解析失败]"
                ret += await self.generate_structed_message_to_creat_context(
                    message_event
                )
            temp += f"这是一条转发消息,内容如下[{ret}]\n"
            DEBUG and print("转发模块json End")
            return temp
        reply_id = self.is_reply(event)
        if reply_id:
            await self._ensure_connected()
            res = await self.nc.get_message(message_id=reply_id)
            if res["status"] == "failed":
                return ""
            message = res["data"]
            ret = ""
            ret += await self.generate_structed_message_to_creat_context(message)
            temp += f"这是一条回复消息,被回复的内容如下[\n\t{ret}\n\t]\n"

        for seg in event.get("message", []):
            seg_type = seg.get("type", "")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                temp += seg_data.get("text", "")
                continue
            if seg_type == "image":
                temp += "图片"
                temp += seg_data.get("summary", "[]")
                url = seg_data.get("url")
                if not url:
                    continue
                if not isOCR:
                    temp += f'\n\t{{"url":{url}}}\n'
                    continue
                ocr_res = (await self.extension.napcat_ocr(url))["text"]
                temp += f"ocr结果{ocr_res}"
                continue
            if seg_type == "at":
                temp += f"[CQ:at,qq={seg_data.get('qq')}]"
        return temp

    async def set_tokenizer(self, event):
        raw_message = event["raw_message"]
        match = re.search(r"True", raw_message)
        if match:
            self.extraconfig["tokenizer"] = True
            await self.send_message(event=event, message="set tokenizer True")
            return True
        match = re.search(r"False", raw_message)
        if match:
            await self.send_message(event=event, message="set tokenizer True")
            self.extraconfig["tokenizer"] = False
            return True
        return False

    async def Check_Campus_NetWoerk(self, event):
        DEBUG = False
        if self.campus_assistant is None:
            DEBUG and print("CampusAssistant 尚未加载完成")
            return
        raw_msg = event["raw_message"]
        message = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg).strip()
        DEBUG and print(f"校园网提问: {message}")
        reply = await self.campus_assistant.chat(
            message=message,
            icl = await self.get_history_msg(event),
            group_id=event.get("group_id"),
        )
        if not reply:
            return
        # 如果助手表示不能回答，静默跳过
        if "[我不能回答]" in reply:
            DEBUG and print("校园网助手判定无法回答，跳过")
            return
        # 去掉 [我能回答] 前缀再发送
        reply = reply.replace("[我能回答]", "").strip()
        await self.send_message(event, reply)
        return

    async def _route_to_campus_assistant(self, event, clean_text: str,iclhistory=None):
        DEBUG = True
        """当主 Agent 返回 __NETWORK__ 信号时，将对话转交校园网助手处理"""
        if self.campus_assistant is None:
            print("CampusAssistant 尚未加载完成，无法转交校园网问题")
            return
        DEBUG and print(f"[转交校园网助手] 用户提问: {clean_text}")
        await self._ensure_connected()
        reply = await self.campus_assistant.chat(
            message=clean_text,
            icl=iclhistory if iclhistory is not None else await self.get_history_msg(event),
            group_id=event.get("group_id"),
        )
        if not reply:
            return
        if "[我不能回答]" in reply:
            DEBUG and print("校园网助手判定无法回答，跳过")
            return
        reply = reply.replace("[我能回答]", "").strip()
        await self.send_message(event, reply)

    async def auto_forward(self, event, groupid_list=None, userid_list=None, target_groupid=None, task_userid=None) -> bool:
        DEBUG = False
        if groupid_list is None:
            groupid_list = []
        if userid_list is None:
            userid_list = []
        if target_groupid is None:
            target_groupid = []
        if task_userid is None:
            task_userid = []
        if event["message_type"] == "group":
            gid = event["group_id"]
            if gid not in groupid_list:
                if DEBUG:
                    DEBUG and print("GROUP_ID NOT CORRECT")
                return False
        elif event["message_type"] == "private":
            uid = event["user_id"]
            if uid not in userid_list:
                if DEBUG:
                    DEBUG and print("USER_ID NOT CORRECT")
                return False
        else:
            return False
        match = self.check_is_old_forward(event) or self.check_is_multimsg_forward(event)
        if not match:
            DEBUG and print("Match Failed!")
            return False
        for group_id in target_groupid:
            if group_id == event["group_id"]:
                continue
            await self._ensure_connected()
            asyncio.create_task( self.nc.forward_group_single_msg(group_id=group_id, message_id=event["message_id"]))
            DEBUG and print("Succeed Forward!")
        for user_id in task_userid:
            await self._ensure_connected()
            asyncio.create_task(self.nc.forward_friend_single_msg(user_id=user_id,message_id=event["message_id"]))
            DEBUG and print("Succeed Forward!")
        return True
    
    async def get_long_history(self, group_id, message_id, remaining: int):
        if remaining <= 0:
            return []

        part = []
        DEBUG = True

        last_msg_id = message_id

        while remaining > 0:
            DEBUG and print("reamaining = ", remaining)
            await self._ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][0]["message_seq"]
            DEBUG and print(icl_history["data"]["messages"])
            DEBUG and print("last_msg_id", last_msg_id)
            icl_history["data"]["messages"].pop()
            part = icl_history["data"]["messages"] + part
            remaining -= 1

        return part

    async def get_history_msg(self, event=None,message_seq=None) -> str:
        await self._ensure_connected()
        icl_history = await self.nc.get_group_msg_history(group_id=event["group_id"],reverse_order=True,message_seq=message_seq or event.get("message_seq",None))
        parts = []
        for ctx in icl_history["data"]["messages"]:
            parts.append(await self.generate_structed_message_to_creat_context(ctx))
        return "\n".join(parts)

    async def get_long_history_test(self, event):
        DEBUG = False
        DEBUG and print("enter History Debug")
        res = await self.get_long_history(
            group_id=event["group_id"], message_id=event["message_id"], remaining=10
        )
        DEBUG and print(res)
        DEBUG and print("end")

    def _init_agent_bg(self):
        """在后台线程中导入并初始化 QQBotAgent / SimpleLLM / CampusAssistant"""
        DEBUG = True
        DEBUG and print("后台预加载 AI Agent …")
        from Agent import QQBotAgent

        self._agent = QQBotAgent(napcat_api=self.nc, extension=self.extension,mp=self)
        # from Agent.SimpleLLM import SimpleChatModule as SLLM

        # self.sllm = SLLM()
        from Agent.CampusAssistant import CampusAssistant

        self.campus_assistant = CampusAssistant(message_processor=self)
        self._agent_ready.set()
        DEBUG and print("AI Agent + CampusAssistant 后台加载完成")

    async def _heartbeat_loop(self):
        """后台心跳协程：每 10~20 分钟（均匀分布）调用 get_login_info 保活"""
        DEBUG = False
        while True:
            interval = random.uniform(10 * 60, 20 * 60)  # 10~20 分钟
            await asyncio.sleep(interval)
            try:
                await self._ensure_connected()
                result = await self.nc.get_login_info()
                if DEBUG:
                    DEBUG and print(
                        f"[心跳] get_login_info 成功: {result.get('data', {}).get('nickname', 'unknown')}"
                    )
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


class image_forward_service:
    def __init__(self, message_processor: MessageProcessor):
        self.ms = message_processor
        self.task_dict = {}
        self.lock = asyncio.Lock()

    async def create_task(self, group_id, target_id_list,msg_type):
        # 加锁，确定不会创建多个任务
        group_id = str(group_id)
        async with self.lock:
            if self.task_dict.get(group_id):
                print("Tasl has been created")
                return False
            self.task_dict[group_id] = {
                "begin_id": None,
                "end_id": None,
                "begin_event": asyncio.Event(),
                "end_event": asyncio.Event(),
            }
        asyncio.create_task(self._task_init(group_id, target_id_list,msg_type))
        return True

    async def _task_init(self, group_id, target_id_list,msg_type):
        group_id = str(group_id)
        data = self.task_dict.get(group_id)
        if not data:
            print("理论上不存在该问题")
            return
        await data["begin_event"].wait()
        await data["end_event"].wait()
        await self.ms.image_forward(
            group_id, target_id_list, data["begin_id"], data["end_id"],msg_type
        )
        async with self.lock:
            self.task_dict.pop(group_id)
        return

    async def set_begin_id(self, group_id, begin_id):
        if not begin_id:
            return
        async with self.lock:
            group_id = str(group_id)
            data = self.task_dict.get(group_id)
            if not data:
                print("任务未创建！")
                return
            data["begin_id"] = begin_id
            data["begin_event"].set()
        return

    async def set_end_id(self, group_id, end_id):
        if not end_id:
            return
        async with self.lock:
            group_id = str(group_id)
            data = self.task_dict.get(group_id)
            if not data:
                print("任务未创建！")
                return
            data["end_id"] = end_id
            data["end_event"].set()
        return
