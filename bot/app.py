"""BotApp —— 组装所有服务，管理生命周期。

它是唯一允许同时认识 transport / messaging / agent / media / download_system /
bot.services / bot.commands 的地方。各子系统之间不互相 import。

启动顺序（由 run.py 调用）：

    settings + images
      -> connections
      -> BotApp(...)
      -> WebServer.start_if_enabled()
      -> listen(...)
      -> BotApp.start()          # 取 self_id、加载断点、心跳、后台 Agent
"""

from __future__ import annotations

import asyncio
import random
import threading
from copy import deepcopy
from typing import Any

from config.connection import ConnectionSettings
from config.settings import BotSettings
from core.anchors import MessageAnchor, anchor_of, anchors_equal
from core.async_utils import cancel, spawn
from core.logging import get_logger
from download_system import DownloadService, DownloadStorage
from images import ImageResolver, configure_from_settings
from media.service import MediaService
from messaging.processor import MessageProcessor
from transport.api import OneBotAPI
from transport.connection import OneBotConnection

from .commands import CommandHandlers
from .keyword import build_patterns
from .pipeline import MessagePipeline
from .services.forward import ForwardService
from .services.interject import InterjectService
from .services.range_task import RangeTaskService

log = get_logger("bot.app")


class BotApp:
    def __init__(
        self,
        *,
        config: ConnectionSettings,
        connection: OneBotConnection,
        settings: BotSettings,
        api_connection: OneBotConnection | None = None,
    ):
        self.config = config
        self.conn = connection
        self.events_connection = connection
        self.api_connection = api_connection or connection
        self.settings = settings

        # ── 图片：全局解析器（业务只通过它拿 NapCat 路径）──
        self.images: ImageResolver = configure_from_settings(settings)

        # ── 协议层 ──
        self.api = OneBotAPI(self.api_connection)

        # ── 下载 / 媒体 ──
        self.downloads = DownloadService(
            DownloadStorage(settings.paths.temp_dir, settings.paths.downloads_dir),
            workers=2,
            task_timeout=600.0,
            max_retries=2,
        )
        self.media = MediaService(self.api, self.images, self.downloads, settings)

        # ── 消息层 ──
        self.mp = MessageProcessor(
            api=self.api,
            images=self.images,
            extraconfig=dict(settings.extraconfig),
            media=self.media,
        )

        # ── 业务服务 ──
        self.forward = ForwardService(
            mp=self.mp, api=self.api, settings=settings, media=self.media
        )
        self.range_tasks = RangeTaskService()
        self.commands = CommandHandlers(
            mp=self.mp,
            api=self.api,
            forward=self.forward,
            ranges=self.range_tasks,
            settings=settings,
            media=self.media,
            downloads=self.downloads,
        )
        self.interject = InterjectService(
            mp=self.mp, settings=settings, agent_getter=self._agent_if_ready
        )

        # ── Agent：后台线程初始化，不阻塞启动 ──
        self._agent = None
        self.campus_assistant = None
        self._agent_ready = threading.Event()
        self._agent_generation = 0
        self._agent_thread: threading.Thread | None = None
        self._heartbeat_task: asyncio.Task | None = None

        self.pipeline = MessagePipeline(
            mp=self.mp,
            settings=settings,
            forward=self.forward,
            interject=self.interject,
            commands=self.commands,
            agent_getter=self.get_agent,
            keyword_builder=self.build_keyword_pattern_config,
            campus_getter=lambda: self.campus_assistant,
        )

        # 断点恢复：启动时跑一次，之后每次断线重连再跑一次。
        # `_recovery_ready` 之前不响应连接回调，避免和 start() 的首次恢复抢跑。
        self._recovery_ready = False
        self._recovering_groups: set[str] = set()
        self._bind_connection_events()

        self._start_agent_background()

    # ════════════════════ 生命周期 ════════════════════
    async def start(self) -> None:
        await self.downloads.start()
        await self.setuserid()
        # 先允许重连回调，再跑首次恢复：如果首次恢复期间正好重连，
        # 回调会再触发一次；recover_from_check_point 内部按群去重。
        self._recovery_ready = True
        await self.recover_from_check_point()

    async def stop(self) -> None:
        self._recovery_ready = False
        if self._heartbeat_task is not None:
            await cancel(self._heartbeat_task)
            self._heartbeat_task = None
        try:
            self.interject.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.downloads.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("关闭下载池失败: %s", exc)
        checkpoint_task = getattr(self.mp, "_checkpoint_save_task", None)
        if checkpoint_task is not None:
            await cancel(checkpoint_task)
        store = getattr(self.forward, "_dedup_store", None)
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    # ════════════════════ 事件入口 ════════════════════
    async def process_message(self, event: dict, is_recover: bool = False) -> None:
        await self.pipeline.process_message(event, is_recover=is_recover)

    async def handler(self, event: dict, is_recover: bool = False) -> None:
        await self.process_message(event, is_recover=is_recover)

    def _bind_connection_events(self) -> None:
        """连接每次连上（含重连）都回调一次；不支持回调的 fake 直接跳过。"""
        add = getattr(self.events_connection, "add_connect_listener", None)
        if callable(add):
            add(self._on_connection_ready)

    async def _on_connection_ready(self) -> None:
        """重连后补跑断点恢复：掉线期间的历史消息不会自己回来。"""
        if not getattr(self, "_recovery_ready", False):
            return
        if getattr(self.mp, "user_id", None) is None:
            return
        if not getattr(self.mp, "check_point", None):
            return
        log.info("[断点恢复] 连接已恢复，检查断点")
        await self.recover_from_check_point()

    # ════════════════════ 自身信息 / 断点 ════════════════════
    async def setuserid(self) -> None:
        if self.mp.user_id is None:
            await self.mp.ensure_connected()
            self.mp.user_id = await self.api.get_user_id()
            check_point = await self.mp.read_check_point(self.mp.user_id)
            self.mp.check_point = check_point or {}
            log.info(
                "[启动] user_id=%s, 加载断点 %s 个",
                self.mp.user_id,
                len(self.mp.check_point),
            )
            self.mp.start_checkpoint_save()
        self.start_heartbeat()

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = spawn(self._heartbeat_loop(), name="heartbeat")
            log.info("心跳任务已启动（间隔 10~20 分钟）")

    async def _heartbeat_loop(self) -> None:
        while True:
            low, high = self.settings.heartbeat_range
            interval = random.uniform(low * 60, high * 60)
            await asyncio.sleep(interval)
            try:
                await self.mp.ensure_connected()
                await self.api.get_login_info()
            except Exception as exc:  # noqa: BLE001
                log.warning("[心跳] 异常: %s", exc)

    async def recover_from_check_point(self) -> None:
        """深拷贝断点，为每个群创建一个回放任务（不阻塞主流程）。

        启动和每次断线重连都会调用。同一个群已经有回放任务时直接跳过：
        重连风暴、启动恢复和重连恢复同时触发时，不会重复搬运同一段历史。
        """
        check_point = deepcopy(self.mp.check_point)
        if not check_point:
            return

        recovering = getattr(self, "_recovering_groups", None)
        if recovering is None:
            recovering = set()
            self._recovering_groups = recovering

        await self.mp.ensure_connected()
        group_list = await self.api.get_group_list()
        groups = group_list.get("data", []) if isinstance(group_list, dict) else []
        group_ids = {int(item["group_id"]) for item in groups if item.get("group_id")}

        for key, event in check_point.items():
            try:
                if int(key) not in group_ids:
                    self.mp.check_point.pop(key, None)
                    log.info("[断点恢复] 群 %s 不在当前群列表，已删除断点", key)
                    continue
            except (TypeError, ValueError):
                self.mp.check_point.pop(key, None)
                continue

            group_key = str(key)
            if group_key in recovering:
                log.info("[断点恢复] 群 %s 已有回放任务，跳过重复触发", key)
                continue

            # 先占位再 await，避免两次恢复同时通过「没在跑」的检查。
            recovering.add(group_key)
            try:
                latest = await self.mp.get_group_latest_msg(group_id=key)
                if latest is None:
                    log.info("[断点恢复] 群 %s 无最新消息，跳过", key)
                    recovering.discard(group_key)
                    continue
                if anchors_equal(latest, event):
                    log.info("[断点恢复] 群 %s 最新消息已是断点，无需回放", key)
                    recovering.discard(group_key)
                    continue
                task = spawn(
                    self.recover_process(event=event, latest=latest),
                    name=f"recover-{key}",
                )

                def _done(done_task, group_key=group_key):
                    recovering.discard(group_key)
                    if done_task.cancelled():
                        return
                    exc = done_task.exception()
                    if exc is not None:
                        log.warning(
                            "[断点恢复] 群 %s 回放失败: %r", group_key, exc
                        )

                task.add_done_callback(_done)
            except Exception:
                recovering.discard(group_key)
                raise

    async def recover_process(
        self,
        event: dict,
        latest: Any = None,
        latest_msg_id: Any = None,
    ) -> None:
        """回放断点消息：从最新往前翻，直到找到断点消息为止。

        ``latest`` 通常是最新一条消息事件；也接受单个游标。旧调用
        ``latest_msg_id=...`` 仍然可用，会同时当作两种游标。
        """
        group_id = event.get("group_id")
        target = anchor_of(event)
        if not target:
            return

        if isinstance(latest, MessageAnchor):
            cursor = latest
        elif isinstance(latest, dict):
            cursor = anchor_of(latest)
        else:
            cursor = MessageAnchor()
            if latest is not None:
                latest_msg_id = latest
        if not cursor and latest_msg_id is not None:
            cursor = MessageAnchor(
                message_seq=latest_msg_id, message_id=latest_msg_id
            )

        remaining = 80
        stalled = 0

        # 游标字段可能是配置猜的。发错字段不会报错，而是静默返回「以锚点
        # 为中心的窗口」并忽略 reverse_order——那会让下面的循环原地打转。
        verify = getattr(self.api, "verify_history_cursor", None)
        if callable(verify):
            try:
                await verify(group_id)
            except Exception as exc:  # noqa: BLE001 - 探测失败不该挡住恢复
                log.warning("[断点恢复] 群 %s 游标探测失败: %s", group_id, exc)

        while remaining > 0:
            remaining -= 1
            await self.mp.ensure_connected()
            history = await self.api.get_group_msg_history(
                group_id=group_id,
                reverse_order=True,
                count=20,
                message_seq=cursor.message_seq,
                message_id=cursor.message_id,
            )
            messages = (history.get("data") or {}).get("messages") or []
            if not messages:
                break

            # reverse_order=True 是升序返回（旧→新），锚点在末位。不按位置猜：
            # 按身份把「作为游标的那条」剔掉，它上一轮已经处理过。
            if cursor:
                kept = [m for m in messages if not anchors_equal(m, cursor)]
                if not kept:
                    log.info("[断点恢复] 群 %s 这一页只有游标本身，停止", group_id)
                    break
                messages = kept

            # 这一页最旧的一条，下一轮用它当游标，保证严格向更旧推进。
            next_cursor = anchor_of(messages[0])

            # reverse_order 返回升序（旧→新），倒序遍历才是最新→最旧
            for msg_event in reversed(messages):
                if anchors_equal(msg_event, target):
                    latest = await self.mp.get_group_latest_msg(group_id)
                    if latest:
                        self.mp.check_point[str(group_id)] = latest
                    log.info(
                        "[断点恢复] 群 %s 找到断点消息 %s",
                        group_id,
                        target.preferred(self.config.history_anchor),
                    )
                    return
                if msg_event.get("user_id") == self.mp.user_id:
                    log.info("[断点恢复] 群 %s 检测到 Bot 自己的消息，停止", group_id)
                    return
                await self.process_message(event=msg_event, is_recover=True)

            # 游标没往前走（多半是字段发错或被后端忽略），别硬翻 80 页。
            if cursor and anchors_equal(next_cursor, cursor):
                stalled += 1
                if stalled >= 2:
                    log.warning(
                        "[断点恢复] 群 %s 游标连续 %s 次没有前进，停止（当前游标 %s）",
                        group_id,
                        stalled,
                        next_cursor.preferred(self.config.history_anchor),
                    )
                    break
            else:
                stalled = 0
            cursor = next_cursor

        log.info("[断点恢复] 群 %s 未找到断点，可能已被清空", group_id)

    # ════════════════════ Agent ════════════════════
    def _start_agent_background(self) -> None:
        # 把当前这一代的 Event 传进线程：重载后旧线程也要 set 旧 Event，
        # 否则正在 await 旧 Event 的消息会永远挂住。
        ready_event = self._agent_ready
        self._agent_thread = threading.Thread(
            target=self._init_agent_bg,
            args=(self._agent_generation, ready_event),
            daemon=True,
            name="agent-loader",
        )
        self._agent_thread.start()

    def _init_agent_bg(self, generation: int = 0, ready_event=None) -> None:
        ready_event = ready_event or self._agent_ready
        if not self.settings.agent_enabled:
            log.info("[Agent] agent.enable=false，跳过加载")
            if generation == self._agent_generation:
                self._agent = None
            ready_event.set()
            return

        try:
            from agent import QQBotAgent

            llm_config = {
                **self.settings.agent_llm_config,
                **self.settings.agent_model_routing,
            }
            agent = QQBotAgent(
                api=self.api,
                messaging=self.mp,
                media=self.media,
                images=self.images,
                settings=self.settings,
                llm_config=llm_config,
            )
            campus = None
            try:
                from agent.campus import CampusAssistant

                campus = CampusAssistant(
                    message_processor=self.mp, llm_config=llm_config
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[CampusAssistant] 加载失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("[Agent] 初始化失败，AI 本次不可用: %s", exc)
            if generation == self._agent_generation:
                self._agent = None
            ready_event.set()
            return

        if generation != self._agent_generation:
            log.info("[Agent] 配置已更新，丢弃这次加载结果")
            # 只唤醒“这一代”的旧 Event；如果调用方没传 Event（直接调用，
            # 比如测试），此时 ready_event 就是当前 Event，不能把它标成已就绪。
            if ready_event is not self._agent_ready:
                ready_event.set()
            return

        self._agent = agent
        self.campus_assistant = campus
        ready_event.set()
        log.info("[Agent] 加载完成")

    async def get_agent(self):
        if not self.settings.agent_enabled:
            return None
        if not self._agent_ready.is_set():
            # 不阻塞事件循环；只让当前消息等
            await asyncio.to_thread(self._agent_ready.wait)
        return self._agent

    def _agent_if_ready(self):
        return self._agent if self._agent_ready.is_set() else None

    def _restart_agent(self) -> None:
        # 先唤醒等旧 Agent 的消息，让它们退回关键词，而不是卡住
        self._agent_ready.set()
        self._agent_generation += 1
        self._agent = None
        self.campus_assistant = None
        self._agent_ready = threading.Event()
        self._start_agent_background()

    # ════════════════════ 热加载 ════════════════════
    def reload_config(self) -> dict:
        """面板保存后重新读盘并热加载；返回本次生效信息。"""
        was_agent = deepcopy(self.settings.section("agent"))
        was_panel = deepcopy(self.settings.section("web_panel"))
        was_connection = (
            self.settings.connection.type,
            self.settings.connection.backend,
        )

        self.settings.reload()
        self.mp.extraconfig.update(self.settings.extraconfig)

        agent_changed = was_agent != self.settings.section("agent")
        if agent_changed:
            self._restart_agent()

        reasons: list[str] = []
        if was_panel != self.settings.section("web_panel"):
            reasons.append("web_panel.* 改动需要重启 Bot（面板仍用启动时的参数）")
        if was_connection != (
            self.settings.connection.type,
            self.settings.connection.backend,
        ):
            reasons.append("连接方式 / 后端改动需要重启 Bot")

        return {
            "path": str(self.settings.path),
            "applied": [
                "keyword_actions",
                "auto_forward",
                "image_forward",
                "jm",
                "prompts",
                "replies",
                "master_qq",
                "heartbeat",
                "message.extraconfig",
                "agent",
                "interject",
            ],
            "agent_reloading": agent_changed,
            "restart_required": bool(reasons),
            "restart_reason": "；".join(reasons),
        }

    # ════════════════════ 关键词 ════════════════════
    def action_registry(self) -> dict:
        def pick(obj, *names):
            for name in names:
                value = getattr(obj, name, None)
                if value is not None:
                    return value
            return None

        registry = {
            "send_message": getattr(self.mp, "send_message", None),
            "send_img": getattr(self.mp, "send_img", None),
            "get_long_history_test": getattr(self.mp, "get_long_history_test", None),
            "set_tokenizer": getattr(self.mp, "set_tokenizer", None),
            "ocr": getattr(self.mp, "ocr", None),
            "export": getattr(self.commands, "export", None),
            "recall": getattr(self.commands, "recall", None),
            "fabric": getattr(self.commands, "fabric", None),
            "get_music": pick(self.commands, "get_music", "GetMusic"),
            "img_forward_create_task": getattr(
                self.commands, "img_forward_create_task", None
            ),
            "jm": getattr(self.commands, "jm", None),
            "try_send_bili_video": getattr(self.mp, "try_send_bili_video", None),
            "try_send_reply_bili_card": getattr(
                self.mp, "try_send_reply_bili_card", None
            ),
        }
        return {name: func for name, func in registry.items() if func is not None}

    def build_keyword_pattern_config(self) -> list[dict]:
        return build_patterns(self.settings, self.action_registry(), self.images)

    # ════════════════════ 给面板看的状态 ════════════════════
    def connection_info(self) -> dict:
        conn = self.events_connection
        return {
            "type": self.config.type,
            "mode": self.config.mode_label,
            "endpoint": self.config.endpoint,
            "backend": self.config.backend,
            "backend_label": self.config.backend_label,
            "history_anchor": self.config.history_anchor,
            "connected": bool(getattr(conn, "is_connected", False)),
            "role": getattr(conn, "role", ""),
        }

    def checkpoint_count(self) -> int:
        try:
            return len(self.mp.check_point)
        except Exception:  # noqa: BLE001
            return 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"BotApp({self.config.mode_label}, user_id={getattr(self.mp, 'user_id', None)})"


__all__ = ["BotApp"]
