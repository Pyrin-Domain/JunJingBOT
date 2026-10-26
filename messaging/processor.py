"""MessageProcessor —— 单条消息级别的文本处理与收发。

只做「消息数据」这一层该做的事：

- 文本提取 / 上下文构建入口：``get_clean_context``、``collect_message``、``build_chat_context``
- 内容判定：``is_AT``、``is_reply``、``is_contain_image``、``is_contain_video``
- 文本匹配与分词：``pattern_match``、``tokenizer``
- 文本 / 图片收发：``send_message``、``send_img``
- 历史消息与断点状态：``get_history_msg``、``get_long_history*``、``check_point``
- 临时会话历史：敏感期 / 续聊

具体实现按职责拆在 :mod:`.history` / :mod:`.forward` / :mod:`.context_builder`，
这里的方法保持旧 ``Minitor/NapCatTools.py`` 的签名与返回值，方便上层零改动。

依赖注入（全部可替换，测试用 fake）：

    MessageProcessor(api=NapCatAPI(...), images=ImageResolver(...),
                     thread_history=ThreadHistoryStore(),
                     extraconfig={"tokenizer": False, "recent": True},
                     media=MediaService(...), ocr=OCR())
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import re
from datetime import datetime
from pathlib import Path

import jieba

from config.paths import AppPaths

from . import forward as forward_mod
from . import history as history_mod
from .context_builder import build_chat_context, collect_message
from .meme import to_segments
from .models import ChatMessage, Segment, Sender, parse_event
from .thread_history import ThreadHistoryStore

# 关键字（bot_config.json）里的图片占位符由 images.ImageResolver.expand 处理，
# 这里不再缓存目录（旧 _IMGS_DIR 是导入期缓存，改配置不生效）。


class MessageProcessor:
    """消息文本处理层：解析、判定、上下文构建与文本/图片收发。"""

    def __init__(
        self,
        api,
        images,
        thread_history=None,
        extraconfig: dict | None = None,
        media=None,
        ocr=None,
    ):
        """
        :param api: NapCat API（``transport.NapCatAPI`` 或测试 fake）
        :param images: :class:`images.ImageResolver`（提供 ``paths`` / ``resolve``）
        :param thread_history: 可选的 ``ThreadHistoryStore``；不传则惰性自建
        :param extraconfig: 运行期开关（``tokenizer`` / ``recent``），与旧版一致
        :param media: 可选 ``MediaService``（OCR / B 站 / 点歌）
        :param ocr: 可选 OCR 服务（``media.ocr.OCR``，有 ``recognize`` 或 ``ocr``）
        """
        self.nc = api
        self.api = api
        self.images = images
        self.media = media
        # 注意：不能叫 self.ocr —— 会覆盖同名的 ocr(event) 方法。
        self.ocr_service = ocr
        self.extension = ocr          # 旧字段名兼容
        self.extraconfig: dict = {"tokenizer": False, "recent": True}
        if extraconfig:
            self.extraconfig.update(extraconfig)
        self.user_id: str | None = None
        self._connect_lock = asyncio.Lock()  # 防止并发重复建连
        self.check_point: dict = {}
        self._checkpoint_save_task: asyncio.Task | None = None
        # 临时的会话历史缓存（按群 / 私聊一份，TTL 到期自动销毁）
        self._thread_history = thread_history

    # ════════════════════ OCR ════════════════════
    async def ocr_text(self, url, re_ocr: bool = False) -> str:
        """交给注入的 OCR 服务识别一张图；没有服务或识别失败返回空串。"""
        service = self.ocr_service if self.ocr_service is not None else self.media
        if service is None:
            return ""
        fn = getattr(service, "recognize", None) or getattr(service, "ocr", None)
        if not callable(fn):
            return ""
        try:
            try:
                result = fn(url, re_ocr=re_ocr)
            except TypeError:
                result = fn(url)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            print(f"[OCR] 识别失败: {exc!r}")
            return ""
        if isinstance(result, dict):
            return str(result.get("text") or "")
        return str(result or "")

    async def ocr_and_send(self, group_id, url):
        ocr_answer = await self.ocr_text(url)
        if not ocr_answer:
            return
        await self.ensure_connected()
        await self.nc.send_group_message(group_id=group_id, message=ocr_answer)

    async def ocr(self, event):
        """回复一条带图消息时，把 OCR 结果发回群里。"""
        reply_id = self.is_reply(event)
        if not reply_id:
            return
        await self.ensure_connected()
        res = await self.nc.get_message(message_id=reply_id)
        msg_event = res.get("data", {}) or {}

        url_list = self.get_url(msg_event)

        for url in url_list:
            asyncio.create_task(self.ocr_and_send(group_id=event["group_id"], url=url))
        return

    # ════════════════════ 消息内容 ════════════════════
    def get_url(self, msg_event) -> list[str]:
        res = []
        for item in (msg_event or {}).get("message", []) or []:
            item_type = item.get("type", "")
            if item_type != "image":
                continue
            url = (item.get("data") or {}).get("url")
            if not url:
                continue
            res.append(url)
        return res

    def is_contain_image(self, msg_event) -> bool:
        for item in (msg_event or {}).get("message", []) or []:
            item_type = item.get("type", "")
            if item_type == "image" and not item.get("data", {}).get("summary"):
                return True
        return False

    def is_contain_video(self, msg_event) -> bool:
        for item in (msg_event or {}).get("message", []) or []:
            item_type = item.get("type", "")
            if item_type == "video":
                return True
        return False

    async def is_AT(self, event: dict, user_id: list[str]) -> list | None:
        """检查消息是否包含 @ 指定用户。"""
        temp = []
        info = event.get("message", []) or []
        for item in info:
            item_type = item.get("type", "")
            if item_type != "at":
                continue
            qq_id = str((item.get("data") or {}).get("qq", ""))
            if qq_id in user_id and qq_id not in temp:
                temp.append(qq_id)
        return temp

    def is_reply(self, msg_event: dict):
        """被引用消息的 message_id（注意：不是 message_seq）。"""
        for seg in (msg_event or {}).get("message", []) or []:
            if seg.get("type") == "reply":
                return seg["data"]["id"]
        return None

    def get_clean_context(self, event, keepAt: bool = False) -> str:
        """纯文本视图：CQ 码清洗、图片留 summary、可选保留 @。"""
        DEBUG = True
        context = ""
        for item in event.get("message", []) or []:
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

    # ════════════════════ 发送 ════════════════════
    async def ensure_connected(self):
        """确保 API 连接可用（委托给 connection，带锁，避免并发重复建连）。"""
        async with self._connect_lock:
            conn = getattr(self.api, "connection", None)
            if conn is not None:
                ensure = getattr(conn, "ensure_connected", None)
                if callable(ensure):
                    await ensure()
                    return
                wait = getattr(conn, "wait_for_connect", None)
                if callable(wait):
                    await wait()
                    return
            ensure = getattr(self.api, "ensure_connected", None)
            if callable(ensure):
                await ensure()
                return
            connect = getattr(self.api, "connect", None)
            if callable(connect):
                await connect()

    async def send_message(self, event, message):
        """根据事件类型发送消息；返回 NapCat 的响应（里面带 message_id，供历史缓存用）。"""
        if isinstance(message, list):
            n = len(message)
            index = random.randint(0, n - 1)
            message = message[index]

        # 带表情包的消息转成消息段数组：mface 的 summary 带方括号，
        # 交给 NapCat 解析 CQ 码容易被截断，段数组不经过 CQ 解析，最稳。
        segments = to_segments(message) if isinstance(message, str) else None

        if self.extraconfig.get("tokenizer") and segments is None:
            message = self.tokenizer(message)

        await self.ensure_connected()
        result = None
        payload = segments if segments is not None else message
        if event["message_type"] == "group":
            result = await self.nc.send_group_message(
                group_id=event["group_id"], message=payload
            )
            asyncio.create_task(self.check_point_active_get(event["group_id"]))
        elif event["message_type"] == "private":
            result = await self.nc.send_private_message(
                user_id=event["user_id"], message=payload
            )
        return result

    async def check_point_active_get(self, group_id):
        await self.ensure_connected()
        latest_msg = await self.get_group_latest_msg(group_id)
        if latest_msg:
            self.check_point[f"{group_id}"] = latest_msg

    async def send_img(self, event, img_addr, summary="君景发图"):
        """按事件类型发送图片；``img_addr`` 支持 ``{imgs_dir}`` / 本地路径 / URL。"""
        if isinstance(img_addr, list):
            n = len(img_addr)
            index = random.randint(0, n - 1)
            img_addr = img_addr[index]

        napcat_path = img_addr
        resolver = self.images
        if resolver is not None and hasattr(resolver, "resolve"):
            napcat_path = resolver.resolve(img_addr, summary=summary).napcat_path

        await self.ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_img(
                group_id=event["group_id"], img_addr=napcat_path, summary=summary
            )
            asyncio.create_task(self.check_point_active_get(event["group_id"]))
        elif event["message_type"] == "private":
            await self.nc.send_private_img(
                user_id=event["user_id"], img_addr=napcat_path, summary=summary
            )

    # ════════════════════ 关键词 ════════════════════
    async def pattern_match(self, event, pattern_config: list[dict]):
        """按 ``keyword_actions`` 匹配并调度处理函数。

        ``filter`` 语义（与旧版一致 + 明确支持 group / private）：

        - ``{"type": "ban", "data": [群号...]}``：这些群里不触发
        - ``{"type": "group", "data": [群号...]}``：只在这些群触发（data 为空 = 所有群）
        - ``{"type": "private", "data": [QQ...]}``：只在私聊触发（data 为空 = 所有私聊）

        同一条消息可以命中多条规则（旧行为：不 return，全部调度）。
        """
        clean_msg = self.get_clean_context(event)
        ret: bool = False
        for pattern in pattern_config:
            solution = pattern.get("solution")
            regex = pattern.get("regex")
            if not solution or not regex:
                continue
            # 复制一份：旧代码 pop 会改掉调用方的配置
            params = dict(pattern.get("params") or {})
            filter_conf = params.pop("filter", None)
            if filter_conf:
                if not self._filter_allows(filter_conf, event):
                    continue
            if re.search(regex, clean_msg, re.I):
                # 自动检测 solution 是否需要 event 参数
                sig = inspect.signature(solution)
                if "event" in sig.parameters:
                    asyncio.create_task(solution(event=event, **params))
                else:
                    asyncio.create_task(solution(**params))
                ret = True
                continue
        return ret

    @staticmethod
    def _filter_allows(filter_conf: dict, event: dict) -> bool:
        kind = str(filter_conf.get("type") or "")
        data = filter_conf.get("data") or []
        data = [str(item) for item in data]
        message_type = str(event.get("message_type") or "")
        if kind == "ban":
            return str(event.get("group_id")) not in data
        if kind == "group":
            if message_type != "group":
                return False
            return not data or str(event.get("group_id")) in data
        if kind == "private":
            if message_type != "private":
                return False
            return not data or str(event.get("user_id")) in data
        return True

    # ════════════════════ 断点 ════════════════════
    @property
    def checkpoint_dir(self) -> Path:
        paths = getattr(self.images, "paths", None)
        if paths is not None:
            return Path(paths.checkpoint_dir)
        return AppPaths.discover().checkpoint_dir

    async def read_check_point(self, user_id):
        file_name = str(self.checkpoint_dir / f"{user_id}.json")
        try:
            with open(file_name, mode="r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print("FileNotFoundError")
            dir_path = Path(file_name).parent
            dir_path.mkdir(parents=True, exist_ok=True)
            with open(file_name, mode="w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=4)
                return {}
        except Exception:
            print("error")
        return {}

    async def _save_check_point(self):
        """将当前断点写入磁盘（JSON 形状：``{group_id: event}``）。"""
        DEBUG = False
        try:
            file_name = self.checkpoint_dir / f"{self.user_id}.json"
            file_name.parent.mkdir(parents=True, exist_ok=True)
            with open(file_name, "w", encoding="utf-8") as f:
                json.dump(self.check_point, f, ensure_ascii=False, indent=4)
            if self.check_point:
                DEBUG and print(
                    f"[断点保存] 已写入 {len(self.check_point)} 条断点到 {file_name}"
                )
        except Exception as e:
            print(f"[断点保存] 异常: {e}")

    async def _checkpoint_save_loop(self):
        """后台每 10 秒将内存中的断点刷入磁盘。"""
        while True:
            await asyncio.sleep(10)
            try:
                await self._save_check_point()
            except Exception as e:
                print(f"[断点保存] 写入失败: {e}")

    def start_checkpoint_save(self):
        """启动断点定时保存任务（需在事件循环中调用）。"""
        if self._checkpoint_save_task is None or self._checkpoint_save_task.done():
            self._checkpoint_save_task = asyncio.create_task(
                self._checkpoint_save_loop()
            )

    # ════════════════════ 历史（委托 messaging.history） ════════════════════
    async def get_group_latest_msg(self, group_id):
        return await history_mod.get_group_latest_msg(self, group_id)

    async def get_long_history(self, group_id, message_id, remaining: int):
        return await history_mod.get_long_history(self, group_id, message_id, remaining)

    async def get_long_history_backward(self, group_id, start_id, end_id):
        return await history_mod.get_long_history_backward(
            self, group_id, start_id, end_id
        )

    async def get_long_history_only_processor(
        self, group_id, start_id, end_id, processor, params
    ) -> None:
        return await history_mod.get_long_history_only_processor(
            self, group_id, start_id, end_id, processor, params
        )

    async def get_history_events(
        self,
        event=None,
        message_seq=None,
        count: int = 6,
        reverse_order: bool = False,
        *,
        message_id=None,
    ) -> list[dict]:
        return await history_mod.get_history_events(
            self, event=event, message_seq=message_seq, message_id=message_id,
            count=count, reverse_order=reverse_order,
        )

    async def collect_history(
        self,
        event=None,
        message_seq=None,
        count: int = 6,
        reverse_order: bool = False,
        *,
        message_id=None,
        master_ids=(),
        now: datetime | None = None,
        expand_quote: bool = True,
    ) -> list:
        return await history_mod.collect_history(
            self,
            event=event,
            message_seq=message_seq,
            message_id=message_id,
            count=count,
            reverse_order=reverse_order,
            master_ids=master_ids,
            now=now,
            expand_quote=expand_quote,
        )

    async def get_history_msg(
        self,
        event=None,
        message_seq=None,
        count: int = 6,
        reverse_order: bool = False,
        *,
        message_id=None,
    ) -> str:
        return await history_mod.get_history_msg(
            self, event=event, message_seq=message_seq, message_id=message_id,
            count=count, reverse_order=reverse_order,
        )

    async def get_long_history_test(self, event):
        return await history_mod.get_long_history_test(self, event)

    # ════════════════════ 转发卡片（委托 messaging.forward） ════════════════════
    def check_is_miniapp(self, msg_event: dict) -> dict:
        return forward_mod.check_is_miniapp(msg_event)

    def check_is_multimsg_forward(self, msg_event: dict) -> str | None:
        return forward_mod.check_is_multimsg_forward(msg_event)

    def check_is_old_forward(self, msg_event: dict) -> bool:
        return forward_mod.check_is_old_forward(msg_event)

    async def get_forward_try(self, msg_event: dict) -> str:
        return await forward_mod.get_forward_try(self, msg_event)

    async def generate_structed_message_to_creat_context(
        self, event, isOCR=False
    ) -> str:
        return await forward_mod.generate_structed_message_to_creat_context(
            self, event, isOCR=isOCR
        )

    async def _get_message_event(self, message_id, cache: dict | None = None):
        return await forward_mod._get_message_event(self, message_id, cache)

    def _forward_count_from_card(self, event) -> int:
        return forward_mod._forward_count_from_card(event)

    async def _fetch_forward_events(self, event) -> tuple[list[dict], int]:
        return await forward_mod._fetch_forward_events(self, event)

    # ════════════════════ 上下文（委托 messaging.context_builder） ════════════════════
    async def collect_message(self, event, **kwargs):
        return await collect_message(self, event, **kwargs)

    async def build_chat_context(
        self,
        event,
        *,
        history=None,
        history_count: int = 10,
        note: str = "",
        note_values: dict | None = None,
        master_ids=(),
        now: datetime | None = None,
        aliases=(),
    ):
        return await build_chat_context(
            self,
            event,
            history=history,
            history_count=history_count,
            note=note,
            note_values=note_values,
            master_ids=master_ids,
            now=now,
            aliases=aliases,
        )

    # ════════════════════ 临时会话历史 ════════════════════
    def thread_history_store(self) -> ThreadHistoryStore:
        """惰性创建 ThreadHistoryStore。"""
        if self._thread_history is None:
            self._thread_history = ThreadHistoryStore()
        return self._thread_history

    @staticmethod
    def thread_id_of(event) -> str:
        """会话标识：群聊 group_xxx，私聊 private_xxx（与 BotApp 的 thread_id 一致）。"""
        if not event:
            return ""
        if event.get("message_type") == "group":
            return f"group_{event.get('group_id')}"
        return f"private_{event.get('user_id')}"

    def remember_thread_message(self, event, *, master_ids=()):
        """每条进来的消息都丢进临时历史（纯数据映射，不发包）。"""
        if not event:
            return None
        try:
            message = parse_event(
                event,
                self_id=str(self.user_id or event.get("self_id") or ""),
                master_ids=master_ids,
            )
        except Exception as exc:  # 缓存失败不能影响主流程
            print(f"[历史缓存] 解析消息失败: {exc}")
            return None
        # 自己发的消息由 remember_sent_message 负责，这里跳过，避免重复
        if message.sender.is_self:
            return None
        return self.thread_history_store().remember(
            self.thread_id_of(event), message
        )

    def remember_sent_message(self, event, text, result=None, *, hot_seconds=None):
        """Bot 发出消息后调用：写进临时历史，并开始「敏感期」。"""
        text = str(text or "").strip()
        if not event or not text:
            return None
        store = self.thread_history_store()
        thread_id = self.thread_id_of(event)
        message_id = ""
        if isinstance(result, dict):
            message_id = (result.get("data") or {}).get("message_id") or ""
        store.remember(
            thread_id,
            ChatMessage(
                message_id=str(message_id or ""),
                time=datetime.now(),
                sender=Sender(qq=str(self.user_id or ""), name="", is_self=True),
                segments=[Segment("text", text=text)],
            ),
        )
        store.mark_bot_spoke(thread_id)
        return message_id

    def thread_is_hot(self, thread_id, *, hot_seconds=None) -> bool:
        """Bot 刚在这个会话说过话吗（敏感期）。"""
        try:
            store = self.thread_history_store()
            if hot_seconds is None:
                return store.is_hot(thread_id)
            return store.is_hot(thread_id, hot_seconds=hot_seconds)
        except Exception:
            return False

    def replies_to_bot(self, event) -> bool:
        """这条消息是不是在回复 / 引用 Bot 自己说过的话（靠临时历史判定，不发包）。"""
        message_id = self.is_reply(event)
        if not message_id:
            return False
        try:
            return self.thread_history_store().is_bot_message(
                self.thread_id_of(event), message_id
            )
        except Exception:
            return False

    # ════════════════════ 分词 ════════════════════
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

    @staticmethod
    def tokenizer(text: str) -> str:
        """中文分词：基于 jieba 分词库，返回空格分隔的字符串。

        示例::

            tokenizer("我是独角兽。")  ->  '我 是 独角兽 。'
        """
        return " ".join(jieba.cut(text))

    # ════════════════════ 被回复消息 ════════════════════
    async def get_replied_message(self, event):
        """获取被回复消息的完整事件。"""
        reply_id = self.is_reply(event)
        if not reply_id:
            return None
        await self.ensure_connected()
        res = await self.nc.get_message(message_id=reply_id)
        if not res or res.get("status") == "failed":
            return None
        return res.get("data")

    # ════════════════════ B 站（薄封装，细节在 media） ════════════════════
    async def _media_call(self, name: str, *args, **kwargs):
        media = self.media
        if media is None:
            return None
        fn = getattr(media, name, None)
        if not callable(fn):
            return None
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            print(f"[B站卡片] media.{name} 失败: {exc!r}")
            return None
        return result

    async def try_send_reply_bili_card(self, event) -> bool:
        """被回复的是 B 站小程序时，下载并发送真正的视频（旧行为）。"""
        if not self.is_reply(event):
            return False

        text = self.get_clean_context(event)
        if text and re.search(
            r"https?://(?:[A-Za-z0-9.-]+\.)?bilibili\.com|https?://b23\.tv/",
            text,
            re.I,
        ):
            return False

        replied_event = await self.get_replied_message(event)
        if not replied_event:
            return False

        miniapp = self.check_is_miniapp(replied_event)
        if not miniapp:
            return False

        link = miniapp.get("link") or miniapp.get("url") or miniapp.get("qqdocurl")
        if not link:
            return False
        print(f"[B站卡片] 检测到回复的 B 站小程序链接: {link}")

        resolved_link = link
        try:
            from download_system.providers.bilibili import BilibiliProvider

            resolved_link = await asyncio.to_thread(
                BilibiliProvider.resolve_video_url, link
            )
        except Exception:
            pass
        if not resolved_link:
            return False

        print(f"[B站卡片] 最终发送源: {resolved_link}")
        await self._media_call("send_bili_video", event, resolved_link)
        return True

    async def try_send_bili_video(self, event) -> bool:
        """文本中含 B 站视频链接时，下载并发送带音频的视频（旧行为）。"""
        text = self.get_clean_context(event)
        print(f"[B站卡片] 检测文本: {text}")
        if not text:
            return False
        cleaned = text.strip()
        if cleaned in ("", "图片"):
            return False

        from download_system.providers.bilibili import BilibiliProvider

        urls = BilibiliProvider.extract_video_urls(cleaned)
        if not urls:
            return False

        try:
            resolved = await asyncio.to_thread(
                BilibiliProvider.resolve_video_url, urls[0]
            )
            print(f"[B站卡片] 解析文本来源: {urls[0]} -> {resolved}")
            await self._media_call("send_bili_video", event, resolved)
            return True
        except Exception as exc:
            print(f"[B站卡片] 文本 URL 解析发送失败: {exc}")
            return False

    async def try_send_bili_card(self, event) -> bool:
        """兼容旧入口。"""
        return await self.try_send_bili_video(event)

    async def send_bili_video(self, event, download_url: str) -> bool:
        if not download_url:
            return False
        return bool(await self._media_call("send_bili_video", event, download_url))

    async def send_bili_card(self, event, ark_data: dict) -> bool:
        if not ark_data:
            return False
        return bool(await self._media_call("send_bili_card", event, ark_data))

    async def get_bili_ark(self, tittle: str, desc: str, picUrl: str, jumpUrl: str):
        """获取 B 站 ark 卡片（媒体细节在 media）。"""
        return await self._media_call("get_bili_ark", tittle, desc, picUrl, jumpUrl)

    async def get_bili_ark_from_text(self, text: str):
        return await self._media_call("get_bili_ark_from_text", text)

    async def get_bili_ark_from_event(self, event):
        text = self.get_clean_context(event)
        info = await self.get_bili_ark_from_text(text)
        print(f"[B站卡片] 解析结果: {info}")
        return info

    async def get_bili_video_download_url(self, text: str) -> str | None:
        result = await self._media_call("get_bili_video_download_url", text)
        return str(result) if result else None


__all__ = ["MessageProcessor"]
