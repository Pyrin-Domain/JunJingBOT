"""
BOTMain —— Bot 主流程（只负责流程与调用）

这一层只做四件事，具体能力都在别的模块里：

1. 生命周期：拿自身 QQ 号、加载断点、心跳保活、后台预加载 AI Agent
2. 断点恢复：recover_from_check_point / recover_process（回放掉线期间的消息）
3. 事件路由：process_message —— 搬运转发 → 关键词 → 被 @ 时交给 AI Agent
4. 调用各服务：ForwardService（搬运去重）、CommandHandlers（命令）、
   image_forward_service（图片批量任务）、CampusAssistant（校园网助手）

模块分工：

- NapCatTools.MessageProcessor   单条消息的文本处理与收发（工具层）
- Websockets                    正向 ws / 反向 ws_re 统一的 WS 连接层
                                 （config.json 的 type 决定；收事件 + 发 API）
- ForwardService                 自动搬运 / 内容去重 / 图片批量搬运
- ImageForwardService            图片批量搬运任务管理（等 -b / -e 两条消息）
- Commands                       聊天命令：export / fabric / GetMusic / img_fd
- BotConfig                      所有可调参数 ←→ bot_config.json（网页版改这个文件）

BOTMain 自己不写死群号、文案、正则：全部读 BotConfig，改参数不用改代码。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import random
import re
import threading
from copy import deepcopy

from Websockets import NapCatBotConfig
from NapCatAPI import NapCatAPIInterface as nc
from extension import Extension

try:  # 兼容 run.py 的扁平 import 和 `Minitor.BOTMain` 包内 import
    from NapCatTools import MessageProcessor, _IMGS_DIR
    from BotConfig import BotConfig, load_bot_config
    from ForwardService import ForwardService
    from ImageForwardService import image_forward_service
    from Commands import CommandHandlers
except ImportError:  # pragma: no cover
    from Minitor.NapCatTools import MessageProcessor, _IMGS_DIR
    from Minitor.BotConfig import BotConfig, load_bot_config
    from Minitor.ForwardService import ForwardService
    from Minitor.ImageForwardService import image_forward_service
    from Minitor.Commands import CommandHandlers


class BOTMain:
    """Bot 主流程：生命周期 / 事件路由 / 调用各服务。"""

    def __init__(self, config: NapCatBotConfig, conn=None, bot_config=None,
                 *, rws=None):
        """
        :param config: NapCatBotConfig 实例（发送 / API 用的那份配置）
        :param conn: NapCatConnection 实例（run.py 建好的连接，正向 / 反向通用）；
                     不传时退回旧行为：用 config 自建正向连接
        :param bot_config: BotConfig 实例 / dict / bot_config.json 路径；
                           缺省读 config/bot_config.json
        :param rws: ``conn`` 的旧参数名（兼容老调用）
        """
        self.config = config
        connection = conn if conn is not None else rws
        self.conn = connection
        self._rws = connection          # 旧字段名，兼容别处引用
        self.bot_config = self._coerce_bot_config(bot_config)

        if connection is None and getattr(config, "is_reverse", False):
            print("[BOTMain] 警告：反向模式(ws_re)下需要传入 conn=NapCatConnection(...)，"
                  "否则 API 调用会失败")
        # 传了连接就复用它（一条连接收事件 + 发 API）；否则用 config 自建正向连接
        self.nc = nc(connection if connection is not None else config)
        self.extension = Extension()

        # ── 各服务：只依赖 文本层 / 接口层 / 配置，互不耦合 ──
        # 单条消息的文本处理 / 收发工具层
        self.mp = MessageProcessor(napcat_api=self.nc, extension=self.extension)
        self.mp.extraconfig.update(self.bot_config.extraconfig)
        # 搬运 + 去重服务
        self.forward = ForwardService(mp=self.mp, nc=self.nc, config=self.bot_config)
        # 图片批量搬运任务管理（等 -b / -e 补齐区间）
        self.ifs = image_forward_service(forward_service=self.forward)
        # 聊天命令：export / fabric / GetMusic / img_fd
        self.commands = CommandHandlers(
            mp=self.mp,
            nc=self.nc,
            forward=self.forward,
            image_forward=self.ifs,
            config=self.bot_config,
        )

        self._agent = None
        self.campus_assistant = None  # 校园网助手，后台线程惰性初始化
        self._agent_ready = threading.Event()
        self._heartbeat_task: asyncio.Task = None
        # 后台线程预加载 AI Agent + CampusAssistant，不阻塞主线程
        self._agent_thread = threading.Thread(target=self._init_agent_bg, daemon=True)
        self._agent_thread.start()

    # ════════════════════ 配置注入 ════════════════════
    @staticmethod
    def _coerce_bot_config(bot_config) -> BotConfig:
        """BotConfig 实例 / dict（临时覆盖）/ 文件路径 / None（读默认 bot_config.json）"""
        if isinstance(bot_config, BotConfig):
            return bot_config
        if isinstance(bot_config, dict):
            return BotConfig(overrides=bot_config)
        return load_bot_config(bot_config)

    # ════════════════════ 关键词 / 命令注册表 ════════════════════
    def _action_registry(self) -> dict:
        """bot_config.json 里 keyword_actions[].action 名 → 处理函数。

        加新命令：这里注册一个名字，再到 bot_config.json 里加一条 regex + params。
        """
        return {
            "send_message": self.mp.send_message,
            "send_img": self.mp.send_img,
            "get_long_history_test": self.mp.get_long_history_test,
            "set_tokenizer": self.mp.set_tokenizer,
            "ocr": self.mp.ocr,
            "export": self.commands.export,
            "fabric": self.commands.fabric,
            "get_music": self.commands.GetMusic,
            "img_forward_create_task": self.commands.img_forward_create_task,
        }

    def build_keyword_pattern_config(self) -> list[dict]:
        """把 bot_config.json 的 keyword_actions 转成 pattern_match 用的配置。

        - action 名对应 self._action_registry() 里的处理函数，未知的跳过并提示
        - params 里的字符串支持 {imgs_dir} 占位符（自动换成图片目录）
        """
        registry = self._action_registry()
        patterns: list[dict] = []
        for item in self.bot_config.keyword_actions:
            action = str(item.get("action") or "")
            solution = registry.get(action)
            if solution is None:
                print(f"[关键词] 未知 action={action!r}（regex={item.get('regex')!r}），已跳过")
                continue
            params = {
                key: (
                    value.replace("{imgs_dir}", _IMGS_DIR)
                    if isinstance(value, str)
                    else value
                )
                for key, value in (item.get("params") or {}).items()
            }
            pattern: dict = {"solution": solution, "regex": item.get("regex")}
            if params:
                pattern["params"] = params
            patterns.append(pattern)
        return patterns

    # ════════════════════ 参数热加载（网页版面板保存后调用）════════════════════
    def reload_config(self) -> dict:
        """重新读 bot_config.json 并热加载，返回本次生效的信息。

        各服务（ForwardService / CommandHandlers）与 BOTMain 共享同一个 BotConfig
        实例，reload() 之后读到的就是新值 —— 关键词 / 搬运 / 提示词 / 回复文案
        下一条消息即生效；只在启动时读的参数需要重启 Bot。
        """
        was_agent_enabled = self.bot_config.agent_enabled
        was_panel = self.bot_config.section("web_panel")
        self.bot_config.reload()
        # 这类开关是在 __init__ 里注入给 MessageProcessor 的，重载时同步一次
        self.mp.extraconfig.update(self.bot_config.extraconfig)

        # 只有「启动时读一次」的参数才需要重启：AI Agent 开关、面板自身的监听参数
        reasons: list[str] = []
        if was_agent_enabled != self.bot_config.agent_enabled:
            reasons.append("agent.enable 改动需要重启 Bot 才会生效")
        if was_panel != self.bot_config.section("web_panel"):
            reasons.append("web_panel.* 改动需要重启 Bot（面板仍用启动时的参数）")

        return {
            "path": str(self.bot_config.path),
            "applied": [
                "keyword_actions",
                "auto_forward",
                "image_forward",
                "prompts",
                "replies",
                "master_qq",
                "heartbeat",
                "message.extraconfig",
            ],
            "restart_required": bool(reasons),
            "restart_reason": "；".join(reasons),
        }
    # ════════════════════ 生命周期 ════════════════════
    async def setuserid(self):
        """获取自身 QQ 号 + 加载断点，并启动后台保活任务"""
        # 确保已获取自己的 QQ 号
        if self.mp.user_id is None:
            await self.mp.ensure_connected()
            self.mp.user_id = await self.nc.get_user_id()
            self.mp.check_point = await self.mp.read_check_point(self.mp.user_id)
            print(
                f"[启动] user_id={self.mp.user_id}, 加载断点 {len(self.mp.check_point)} 个"
            )
            # 启动断点定时保存任务
            self.mp.start_checkpoint_save()
        # 启动心跳保活任务
        self.start_heartbeat()
        return

    def start_heartbeat(self):
        """启动心跳后台任务（需在事件循环中调用）"""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            print("心跳任务已启动（间隔 10~20 分钟）")

    async def _heartbeat_loop(self):
        """后台心跳协程：每 10~20 分钟（均匀分布）调用 get_login_info 保活"""
        DEBUG = False
        while True:
            low, high = self.bot_config.heartbeat_range
            interval = random.uniform(low * 60, high * 60)  # 分钟数来自 bot_config.json
            await asyncio.sleep(interval)
            try:
                await self.mp.ensure_connected()
                result = await self.nc.get_login_info()
                if DEBUG:
                    DEBUG and print(
                        f"[心跳] get_login_info 成功: {result.get('data', {}).get('nickname', 'unknown')}"
                    )
            except Exception as e:
                print(f"[心跳] 异常: {e}")

    def _init_agent_bg(self):
        """在后台线程中导入并初始化 QQBotAgent / CampusAssistant"""
        DEBUG = True
        DEBUG and print("后台预加载 AI Agent …")
        if not self.bot_config.agent_enabled:
            print("[Agent] bot_config.json 里 agent.enable=false，跳过加载 AI Agent")
            self._agent_ready.set()
            return
        from Agent import QQBotAgent

        self._agent = QQBotAgent(
            napcat_api=self.nc, extension=self.extension, mp=self.mp
        )
        # from Agent.SimpleLLM import SimpleChatModule as SLLM

        # self.sllm = SLLM()
        from Agent.CampusAssistant import CampusAssistant

        self.campus_assistant = CampusAssistant(message_processor=self.mp)
        self._agent_ready.set()
        DEBUG and print("AI Agent + CampusAssistant 后台加载完成")

    @property
    def agent(self):
        """返回 QQBotAgent，如果后台加载尚未完成则等待"""
        self._agent_ready.wait()
        return self._agent

    async def recover_from_check_point(self):
        """深拷贝断点，创建协程回放（不阻塞主流程）"""
        DEBUG = True
        check_point = deepcopy(self.mp.check_point)
        latest_msg_sqes = {}
        for key, value in check_point.items():
            temp = await self.mp.get_group_latest_msg(group_id=key)
            if temp is None:
                DEBUG and print(f"[断点恢复] 群 {key} 无最新消息，跳过恢复")
                continue
            latest_msg_sqes[key] = {
                "message_seq": temp["message_seq"],
                "real_id": temp.get("real_id"),
                "real_seq": temp.get("real_seq"),
            }

        for key, value in check_point.items():
            if key not in latest_msg_sqes:
                continue
            DEBUG and print(f"[断点恢复] 创建恢复任务: {key}")
            DEBUG and print(f"[断点恢复] 最新消息 seq: {latest_msg_sqes[key]}")
            if latest_msg_sqes[key]["real_id"] == value.get(
                "real_id"
            ) or latest_msg_sqes[key]["real_seq"] == value.get("real_seq"):
                DEBUG and print(
                    f"[断点恢复] 最新消息已是断点消息 {latest_msg_sqes[key]}，无需回放"
                )
                continue
            asyncio.create_task(
                self.recover_process(
                    event=value, latest_msg_id=latest_msg_sqes[key]["message_seq"]
                )
            )
        # if check_point:
        #     await self._save_check_point()

    async def recover_process(self, event, latest_msg_id=None):
        """回放断点消息：从最新消息往前翻，直到找到断点消息为止"""
        DEBUG = True
        group_id = event["group_id"]
        rid = event.get("real_id")
        real_seq = event.get("real_seq")
        if not rid and not real_seq:
            return
        remaining = 8  # 最多翻 8 页
        message_seq = latest_msg_id  # None = 从最新开始
        while remaining > 0:
            remaining -= 1
            await self.mp.ensure_connected()
            history_list = await self.nc.get_group_msg_history(
                group_id=group_id, reverse_order=True, count=20, message_seq=message_seq
            )
            messages = history_list["data"]["messages"]
            if not messages:
                break

            # 记录本次传入的锚点，后面会覆盖 message_seq
            seq_for_this_page = message_seq

            # 翻页锚点 = 本批最旧的消息的 seq（下一次拉更旧的）
            message_seq = messages[0]["message_seq"]

            # 非首次调用：API 返回会包含锚点消息（上一批已处理），pop 掉避免重复
            if seq_for_this_page is not None:
                messages.pop(0)
            if not messages:
                break

            # 从最新→最旧遍历（reverse_order 返回升序 旧→新）
            for msg_event in reversed(messages):
                if (
                    msg_event.get("real_id") == rid
                    or msg_event.get("real_seq") == real_seq
                ):
                    DEBUG and print(f"[断点恢复] {group_id}找到断点消息 {rid}")
                    latest = await self.mp.get_group_latest_msg(group_id)
                    if latest:
                        self.mp.check_point[f"{group_id}"] = latest
                    return
                if msg_event.get("user_id") == self.mp.user_id:
                    DEBUG and print(f"[断点恢复] 断点保存出错，检测到君景消息")
                    return
                    # 被踢期间的消息确实没处理过，走完整 process_message
                    # DEBUG and print(f'[断点恢复] 未找到断点 {rid}，处理消息 {msg_event.get("real_id")}')
                await self.process_message(event=msg_event, is_recover=True)
        print(f"[断点恢复] 未找到断点 {rid}，可能已被清理")

    async def handler(self, event: dict, is_recover: bool = False):
        """事件入口：交给主流程处理"""
        await self.process_message(event=event, is_recover=is_recover)

    async def process_message(self, event, is_recover=False):
        """主流程：断点记录 → 自动搬运转发 → 关键词匹配 → AI Agent"""
        DEBUG = False
        # 每条消息进来立即记录断点（崩了也知道处理到哪条了）
        if event.get("message_type") == "group":
            self.mp.check_point[str(event.get("group_id"))] = event

        message_id = event["message_id"]
        user_id = event["user_id"]
        master_qq = self.bot_config.master_qq
        # raw_msg = event["raw_message"]

        if self.bot_config.auto_forward_enabled and await self.forward.auto_forward(
            event=event,
            is_recover=is_recover,
            **self.bot_config.auto_forward_kwargs,
        ):
            return
        # 关键词快速匹配（不走 AI，省 token）
        if not is_recover:
            asyncio.create_task(
                self.mp.pattern_match(
                    event=event, pattern_config=self.build_keyword_pattern_config()
                )
            )

        # if await self.pattern_match(
        #     event=event,
        #     pattern_config=[
        #         {"regex": r"校园网", "solution": self.Check_Campus_NetWoerk}
        #     ],
        # ):
        #     return

        # 被 @ 时 → 交给 AI Agent 处理
        # return
        ated_user = await self.mp.is_AT(event=event, user_id=[self.mp.user_id])
        if ated_user:
            # 去除 CQ 码，提取纯文本
            clean_text = self.mp.get_clean_context(event, True)
            match = re.match(
                self.bot_config.prompt("history_command_regex"), clean_text
            )
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
            if self.mp.extraconfig["recent"]:
                await self.mp.ensure_connected()
                iclhistory = await self.mp.get_history_msg(
                    event, reverse_order=True, message_seq=event["message_seq"]
                )
                print(iclhistory)
            context = (
                (
                    self.bot_config.prompt("group_context").format(
                        group_id=event["group_id"],
                        user_id=user_id,
                        message_id=message_id,
                        ated_user=ated_user,
                        self_id=self.mp.user_id,
                        master_id=master_qq,
                    )
                    if event["message_type"] == "group"
                    else self.bot_config.prompt("private_context").format(
                        user_id=user_id
                    )
                ),
                (
                    self.bot_config.prompt("history_context").format(
                        history=str(iclhistory)
                    )
                    if iclhistory
                    else ""
                ),
            )
            isDom = (
                "{isDom:true}" if str(user_id) == master_qq else "{isDom:false}"
            )
            DEBUG and print(str(user_id) + isDom)
            DEBUG and print(f"[AI] 用户 {user_id} 提问: {self.mp.generate_structed_message_to_creat_context(event=event,isOCR=True)}")
            totoal_msg = await self.mp.generate_structed_message_to_creat_context(
                event=event
            )
            agent = self.agent
            if agent is None:
                # AI 被关掉（bot_config.json: agent.enable=false）
                return
            reply = await agent.chat(
                event=event,
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
                self.mp.send_message(
                    event=event, message=self.bot_config.reply("network_upgrade")
                )
                # return
                # 主 Agent 判定为校园网问题，转交校园网助手
                await self._route_to_campus_assistant(
                    event, clean_text, iclhistory=iclhistory
                )
            elif reply == "__SILENT__":
                # 主 Agent 判定为不需要回复，静默处理
                return
            elif reply and "已成功发送" not in reply:
                await self.mp.send_message(event, reply)
                return

    async def history_solution(self, event):
        message_id = event["message_id"]
        # 注意：get_message_history 在重构前就已被注释掉，这里保持原有行为
        history = await self.get_message_history(message_id)
        print(history)
        await self.mp.send_message(event, history["context"])

    async def Check_Campus_NetWoerk(self, event):
        DELETED = False
        if DELETED:
            return
        clean_text = self.mp.get_clean_context(event=event)
        await self._route_to_campus_assistant(event=event, clean_text=clean_text)

    async def _route_to_campus_assistant(self, event, clean_text: str, iclhistory=None):
        DELETED = False
        if DELETED:
            return
        DEBUG = True
        """当主 Agent 返回 __NETWORK__ 信号时，将对话转交校园网助手处理"""
        if self.campus_assistant is None:
            print("CampusAssistant 尚未加载完成，无法转交校园网问题")
            return
        DEBUG and print(f"[转交校园网助手] 用户提问: {clean_text}")
        await self.mp.ensure_connected()
        reply = await self.campus_assistant.chat(
            message=clean_text,
            icl=(
                iclhistory
                if iclhistory is not None
                else await self.mp.get_history_msg(event)
            ),
            group_id=event.get("group_id"),
        )
        if not reply:
            return
        if "[我不能回答]" in reply:
            DEBUG and print("校园网助手判定无法回答，跳过")
            return
        reply = reply.replace("[我能回答]", "").strip()
        await self.mp.send_message(event, reply)
