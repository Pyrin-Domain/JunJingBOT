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

from AbstractionLayer.cursor import MessageCursor
from config.connection import ConnectionSettings
from config.settings import BotSettings
from core.anchors import anchor_of
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
from .services.psychology import PsychologyService
from .services.range_task import RangeTaskService

log = get_logger("bot.app")

#: 断点恢复最多同时跑几个群。SnowLuma 的历史接口是串行节流的（实测每次服务端
#: 拉取约 4 秒），一次把 10+ 个群全放出去，后面的请求会排在默认 30 秒超时之后。
_RECOVERY_CONCURRENCY = 3

#: 断点恢复每页拉多少条。20 是实测下来两端都稳的值（NapCat 大了会超时）。
_RECOVERY_PAGE_SIZE = 20

#: 断点恢复最多翻多少页。80 x 20 = 1600 条，够覆盖一次重启的空档。
_RECOVERY_MAX_PAGES = 80

#: Agent 线程等收藏表情并进表情库的最长等待秒数。
#:
#: 收藏表情要等后端连上才抓得到，而提示词里的表情目录必须在建 Agent 之前
#: 就位（模型得知道能写哪些名字）。后端迟迟连不上时也不能把 Agent 一直卡住，
#: 所以给个上限：超时后按现有表情包照常加载。
_CUSTOM_FACES_WAIT = 15.0


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
        # 心理分析：独立模型 + 关键词唤醒；和主对话 / 搬运链路只共用过滤门。
        self.psychology = PsychologyService(
            mp=self.mp,
            api=self.api,
            settings=settings,
            forward=self.forward,
            ocr_text=self.mp.ocr_text,
        )

        # ── Agent：后台线程初始化，不阻塞启动 ──
        self._agent = None
        self.campus_assistant = None
        self._agent_ready = threading.Event()
        self._agent_generation = 0
        self._agent_thread: threading.Thread | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # ── 收藏表情（账号自定义表情）──
        # 抓取要等后端连上，所以由 start() 触发；Agent 建提示词前得等它，
        # 否则提示词里的表情目录没有这些名字，模型压根不知道能写什么。
        self._custom_faces_ready = threading.Event()
        self._custom_faces_count = 0

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
        # 收藏表情要先并进表情库，Agent 的提示词目录里才有这些名字
        await self._sync_custom_faces()
        # 先允许重连回调，再跑首次恢复：如果首次恢复期间正好重连，
        # 回调会再触发一次；recover_from_check_point 内部按群去重。
        self._recovery_ready = True
        await self.recover_from_check_point()

    async def _sync_custom_faces(self) -> int:
        """抓账号的收藏表情，并进表情包库。

        收藏表情不在配置文件里，是运行时从后端 ``fetch_custom_face_detail``
        抓的（两个后端字段名不一样，归一化在 :mod:`AbstractionLayer.face`）。
        名字用用户自己写的备注，模型就能像用静态表情包一样写 ``<meme>备注</meme>``。

        抓不到不算错误：表情包只是加分项，不能连累启动。所以
        ``_custom_faces_ready`` 无论如何都会置位，Agent 不会一直等着。

        注意置位的时机：**必须在合并之后**。Agent 线程拿到这个信号就去
        建提示词（建提示词会 ``configure()`` 重载表情库），信号早一步发出去，
        提示词里的表情目录就没有这些名字了。
        """
        count = 0
        try:
            faces = await self.api.fetch_custom_faces()
            if faces:
                from messaging.meme import merge_custom_faces

                merge_custom_faces(faces)
                count = len(faces)
                self._custom_faces_count = count
                log.info("[表情] 已并入 %d 个收藏表情", count)
        except Exception as exc:  # noqa: BLE001 - 抓不到表情不该影响启动
            log.warning("[表情] 抓收藏表情失败: %s", exc)
        finally:
            self._custom_faces_ready.set()
        return count

    async def stop(self) -> None:
        self._recovery_ready = False
        # 让还在加载的 Agent 线程在返回时丢弃结果，并停止当前 Agent 的后台任务。
        self._agent_generation += 1
        self._agent_ready.set()
        self._close_agent(getattr(self, "_agent", None))
        self._agent = None
        if self._heartbeat_task is not None:
            await cancel(self._heartbeat_task)
            self._heartbeat_task = None
        mp_close = getattr(self.mp, "close", None)
        if callable(mp_close):
            await mp_close()
        else:
            checkpoint_task = getattr(self.mp, "_checkpoint_save_task", None)
            if checkpoint_task is not None:
                await cancel(checkpoint_task)
        try:
            self.interject.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.downloads.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("关闭下载池失败: %s", exc)
        forward_close = getattr(self.forward, "close", None)
        if callable(forward_close):
            try:
                forward_close()
            except Exception:  # noqa: BLE001
                pass
        else:
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

        # 每个群第一次翻历史都会先探测游标。并发探测会同时打出十几条历史
        # 请求，在 SnowLuma 的串行节流下排到默认 30 秒超时之后。先探测一次，
        # 后面的调用直接命中缓存；顺便让 kernel 的能力表按实测结果更新。
        verify = getattr(self.api, "verify_history_cursor", None)
        if callable(verify):
            probe_group = None
            for key in check_point:
                try:
                    candidate = int(key)
                except (TypeError, ValueError):
                    continue
                if candidate in group_ids:
                    probe_group = candidate
                    break
            if probe_group is not None:
                try:
                    await verify(probe_group)
                except Exception as exc:  # noqa: BLE001 - 探测失败不该挡住恢复
                    log.warning("[断点恢复] 游标探测失败: %s", exc)

        # 断点恢复改两阶段：各群回放任务只入库、不发送；等所有任务都结束才 flush
        # （统一算目标群 → 发送）。否则同一份聊天记录从 A、B 两个群同时回放时，
        # 两边都会在对方认领落库之前判「没搬过」，目标群各收一遍。
        #
        # 协调者占一份配额（pending=1），每个群任务各占一份（pending+=1）。
        # 所有群任务结束后 pending 回到 1，协调者再 end 才把 pending 降到 0，触发 flush。
        coordinator = self.forward.begin_recovery_batch()
        try:
            tasks = await self._recover_groups(
                check_point=check_point,
                group_ids=group_ids,
                recovering=recovering,
                verify=verify,
                batch=coordinator,
            )
            # 必须等所有群的回放任务真正结束（它们只入库、不发送），
            # 才能进入第二阶段；否则「入库完成」这个前提不成立。
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.forward.end_recovery_batch(coordinator)

    async def _recover_groups(
        self,
        *,
        check_point: dict,
        group_ids: set,
        recovering: set,
        verify,
        batch,
    ) -> list:
        """为每个有断点的群派一个回放任务（真正发送在批次收尾时）。"""
        tasks: list = []
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
                # 策略由后端能力决定，不在这里判断后端名字：
                #
                # - NapCat：无游标取到的是服务端真·最新，但 message_id /
                #   message_seq 每次重启都会重新分配，断点里的旧 id 不能当
                #   游标 —— 只能先取最新，再往回翻到断点（用 real_id 等
                #   跨重启稳定的字段比对）。
                # - SnowLuma：message_id 是 SHA1(sequence:group:event)，重启后
                #   仍然有效；但无游标只返回它本地观测过的最新消息（关闭登录
                #   历史同步时正好等于断点）—— 必须从断点往后翻。
                recover_kwargs: dict[str, Any] = {}
                if self._history_latest_is_authoritative():
                    latest = await self.mp.get_group_latest_msg(group_id=key)
                    if latest is None:
                        log.info("[断点恢复] 群 %s 无最新消息，跳过", key)
                        recovering.discard(group_key)
                        continue
                    if MessageCursor.from_message(latest).matches(event):
                        log.info("[断点恢复] 群 %s 最新消息已是断点，无需回放", key)
                        recovering.discard(group_key)
                        continue
                    recover_kwargs["latest"] = latest

                # 每个群任务各持一份批次配额：最后一个结束的才会 flush。
                self.forward.begin_recovery_batch()
                task = spawn(
                    self._run_recovery(event=event, **recover_kwargs),
                    name=f"recover-{key}",
                )

                def _done(done_task, group_key=group_key, b=batch):
                    recovering.discard(group_key)
                    # 无论成功、失败还是被取消，都要归还配额，否则批次永远不 flush。
                    spawn(
                        self.forward.end_recovery_batch(b),
                        name=f"recover-done-{group_key}",
                    )
                    if done_task.cancelled():
                        return
                    exc = done_task.exception()
                    if exc is not None:
                        log.warning(
                            "[断点恢复] 群 %s 回放失败: %r", group_key, exc
                        )

                task.add_done_callback(_done)
                tasks.append(task)
            except Exception:
                recovering.discard(group_key)
                raise
        return tasks

    async def _run_recovery(self, event: dict, **kwargs: Any) -> None:
        """在并发闸门内跑一次恢复。

        后端的群历史接口通常一次只能处理一个服务端拉取。把所有群同时放出去
        只会让请求排队，排在后面的超过 ``call_api`` 的默认 30 秒超时。
        """
        gate = getattr(self, "_recovery_gate", None)
        loop = asyncio.get_running_loop()
        if gate is None or getattr(self, "_recovery_gate_loop", None) is not loop:
            gate = asyncio.Semaphore(_RECOVERY_CONCURRENCY)
            self._recovery_gate = gate
            self._recovery_gate_loop = loop
        async with gate:
            await self.recover_process(event=event, **kwargs)

    def _history_latest_is_authoritative(self) -> bool:
        """无游标取到的「最新一条」是不是服务端真·最新。

        优先用协议内核里实测出来的能力；测试或精简调用没有 kernel 时，按
        配置的 backend 查能力表。
        """
        caps = getattr(getattr(self.api, "kernel", None), "capabilities", None)
        if caps is None:
            from AbstractionLayer.kernal import capabilities_for

            caps = capabilities_for(getattr(self.config, "backend", None))
        return bool(getattr(caps, "history_latest_is_authoritative", True))

    def _history_call(self, action: str, params: dict):
        """把 ``self.api`` 包成抽象层能用的发包函数。

        真实连接走 ``_call_api``：游标字段必须由 :class:`HistoryPager` 按实测能力
        挑，再经 ``api._history_params`` 二次映射会把 ``message_id`` 的值顶到
        ``message_seq`` 上——后端不报错，只返回居中窗口，翻页就会原地打转。
        测试 / 精简调用只有具名方法，退回按方法名调用。
        """
        raw = getattr(self.api, "_call_api", None)
        if callable(raw):
            return raw(action=action, params=params)
        handler = getattr(self.api, action, None)
        if callable(handler):
            return handler(**params)
        raise AttributeError(f"api 不支持动作 {action}")

    def _history_pager(self):
        """断点恢复用的分页器（懒建 + 复用，保住实测出来的能力）。"""
        pager = getattr(self, "_pager_cache", None)
        if pager is not None:
            return pager

        from AbstractionLayer import CallableTransport, HistoryPager, OneBotKernel
        from AbstractionLayer.kernal import capabilities_for

        backend = getattr(self.config, "backend", "") or ""
        kernel = getattr(self.api, "kernel", None)
        if kernel is None:
            # 精简调用（测试）没有协议内核：按配置的 backend 查能力表临时建一个。
            kernel = OneBotKernel(
                CallableTransport(self._history_call, label="bot-api"),
                capabilities=capabilities_for(backend),
                label=backend or "bot-api",
            )
        pager = HistoryPager(
            kernel,
            default_count=_RECOVERY_PAGE_SIZE,
            # 翻页途中可能掉线：每一页发出前先确保连接可用（旧实现是在循环里做的）。
            before_page=getattr(self.mp, "ensure_connected", None),
        )
        self._pager_cache = pager
        return pager

    async def _probe_history_cursor(self, group_id) -> None:
        """让协议内核实测一次游标字段；失败不影响恢复。"""
        verify = getattr(self.api, "verify_history_cursor", None)
        if not callable(verify):
            return
        try:
            await verify(group_id)
        except Exception as exc:  # noqa: BLE001 - 探测失败不该挡住恢复
            log.warning("[断点恢复] 群 %s 游标探测失败: %s", group_id, exc)

    async def recover_process(
        self,
        event: dict,
        latest: Any = None,
        latest_msg_id: Any = None,
    ) -> None:
        """回放断点之后的消息。

        翻页方向不在这里判断，交给 :meth:`HistoryPager.replay` —— 调用方给不给
        ``latest`` 就决定了方向（判据见
        ``BackendCapabilities.history_latest_is_authoritative``）：

        - 给了 ``latest``：从最新往回翻到断点（NapCat 的 id 重启后会变，
          只能用现取的最新 id 起翻，靠 ``real_id`` 认断点）；
        - 没给：从断点往后翻（SnowLuma 的 ``message_id`` 稳定，但「最新」可能
          只是本地观测值）。

        这里只做两件业务上的事：**回放顺序**（新 → 旧，遇到自己的消息就停）和
        **断点推进**。
        """
        group_id = event.get("group_id")
        if not anchor_of(event):
            return
        # 旧调用只给了 id：同时当两种游标用（两个后端各认一个）。
        if latest is None and latest_msg_id is not None:
            latest = {"message_seq": latest_msg_id, "message_id": latest_msg_id}

        await self._probe_history_cursor(group_id)

        result = await self._history_pager().replay(
            group_id,
            checkpoint=event,
            latest=latest,
            count=_RECOVERY_PAGE_SIZE,
            max_pages=_RECOVERY_MAX_PAGES,
        )
        if result.reason:
            log.warning("[断点恢复] 群 %s 翻页提前收尾: %s", group_id, result.reason)

        backward = latest is not None
        messages = list(result.messages)
        if backward and isinstance(latest, dict):
            # 起翻用的 ``latest`` 被分页器当锚点剔掉了（锚点永远 exclusive），
            # 但它自己**也是断点之后才到的消息**。不补回来就有个静默丢消息的坑：
            # 找到断点后会把断点推进到最新，于是这条再也不会被回放。
            # 补的判据：不等于断点、也不在已收集的里面。
            latest_cursor = MessageCursor.from_message(latest)
            same_as_checkpoint = latest_cursor.matches(MessageCursor.from_message(event))
            if latest_cursor and not same_as_checkpoint and not any(
                MessageCursor.from_message(item).matches(latest_cursor) for item in messages
            ):
                messages.append(latest)

        if not messages:
            if backward:
                log.info("[断点恢复] 群 %s 未找到断点，可能已被清空", group_id)
            else:
                log.info("[断点恢复] 群 %s 断点之后没有新消息", group_id)
            return

        replayed = 0
        stopped_at_self = False
        for msg_event in reversed(messages):
            if msg_event.get("user_id") == self.mp.user_id:
                log.info("[断点恢复] 群 %s 检测到 Bot 自己的消息，停止", group_id)
                stopped_at_self = True
                break
            await self.process_message(event=msg_event, is_recover=True)
            replayed += 1

        if not backward:
            # 往后翻：messages 升序，最后一条就是这次能看到的最新消息。即使中途
            # 遇到 Bot 自己的消息提前停，也把断点推进到最新，避免下次重复搬运。
            newest = messages[-1]
            if anchor_of(newest):
                self.mp.check_point[str(group_id)] = newest
            log.info(
                "[断点恢复] 群 %s 回放 %s 条到 %s",
                group_id,
                replayed,
                anchor_of(newest).preferred(self.config.history_anchor),
            )
            return

        if not result.found:
            log.info("[断点恢复] 群 %s 未找到断点，可能已被清空", group_id)
            return
        if stopped_at_self:
            # 还没翻到断点就撞上自己的消息：这一段没对齐，断点保持原样（旧行为）。
            return

        # 找到断点说明已经对齐，顺手把断点推进到服务端当前最新。
        latest_event = await self.mp.get_group_latest_msg(group_id)
        if latest_event:
            self.mp.check_point[str(group_id)] = latest_event
        log.info(
            "[断点恢复] 群 %s 找到断点消息，回放 %s 条",
            group_id,
            replayed,
        )

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

        # 等收藏表情并进表情库再建提示词：目录里得有这些名字，
        # 模型才知道能写 <meme>备注名</meme>。后端连不上时最多等这么久，
        # 之后照常加载（没有收藏表情而已，不该卡住整个 Agent）。
        #
        # 用 getattr 而不是 self._xxx：测试会拿 BotApp.__new__ 造半成品实例，
        # 那上面没有这个事件——没有就不等。
        faces_ready = getattr(self, "_custom_faces_ready", None)
        if faces_ready is not None and not faces_ready.wait(timeout=_CUSTOM_FACES_WAIT):
            log.info("[Agent] 等待收藏表情超时，按现有表情包继续加载")

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
        self._close_agent(self._agent)
        self._agent_generation += 1
        self._agent = None
        self.campus_assistant = None
        self._agent_ready = threading.Event()
        self._start_agent_background()

    def _close_agent(self, agent) -> None:
        close = getattr(agent, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - 停机清理不能反过来打断主流程
            log.warning("关闭 Agent 后台任务失败: %s", exc)

    # ════════════════════ 热加载 ════════════════════
    def reload_config(self) -> dict:
        """面板保存后重新读盘并热加载；返回本次生效信息。

        对比基准是「上次从磁盘读进来的配置」（``settings.loaded()``），不是
        ``settings.section()`` —— 面板保存的顺序是 ``update()``（改内存）→
        ``save()``（写盘）→ 这里，等走到这里时内存里已经是新值了，拿它当基准
        永远比不出差异，热加载会静默不生效。
        """
        snapshot = self.settings.loaded()
        was_agent = deepcopy(snapshot.get("agent") or {})
        was_panel = deepcopy(snapshot.get("web_panel") or {})
        was_thread = deepcopy(snapshot.get("thread_memory") or {})
        was_connection = (
            self.settings.connection.type,
            self.settings.connection.backend,
        )

        self.settings.reload()
        self.mp.extraconfig.update(self.settings.extraconfig)
        # 图片送达库把开关 / 保留天数缓存在 store 里；热加载后重建，
        # 否则面板改了要等重启才生效。
        reload_image_dedup = getattr(
            getattr(self, "forward", None), "reload_image_dedup", None
        )
        if callable(reload_image_dedup):
            reload_image_dedup()

        agent_changed = was_agent != self.settings.section("agent")
        # 多轮留存的开关 / 灰度是 Agent 初始化时读的，改完不重建就得重启进程。
        # 重建会丢掉内存里的留存段（ThreadTurns）和工具结果暂存（ResultVault），
        # 这是改配置的必然代价，比“改了没生效”好。
        thread_changed = was_thread != self.settings.section("thread_memory")
        if agent_changed or thread_changed:
            self._restart_agent()

        # 心理分析的模型缓存 / 冷却跟着配置走；不重建的话面板改完要重启。
        psychology = getattr(self, "psychology", None)
        if psychology is not None:
            reload_psychology = getattr(psychology, "reload", None)
            if callable(reload_psychology):
                reload_psychology()

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
                "forward_image_dedup",
                "image_forward",
                "jm",
                "prompts",
                "replies",
                "master_qq",
                "heartbeat",
                "message.extraconfig",
                "agent",
                "interject",
                "thread_memory",
                "psychology",
            ],
            "agent_reloading": agent_changed or thread_changed,
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
            "psychology": getattr(getattr(self, "psychology", None), "handle", None),
            "sex_filter_test": getattr(self.commands, "sex_filter_test", None),
            "audit_release": getattr(self.commands, "audit_release", None),
            "try_send_bili_video": getattr(self.mp, "try_send_bili_video", None),
            "try_send_reply_bili_card": getattr(
                self.mp, "try_send_reply_bili_card", None
            ),
        }
        return {name: func for name, func in registry.items() if func is not None}

    def build_keyword_pattern_config(self) -> list[dict]:
        patterns = build_patterns(self.settings, self.action_registry(), self.images)
        # 心理分析的关键词集中在新配置段里维护（psychology.keywords），
        # 不塞进 keyword_actions：面板上是一张独立页面，改一处就够。
        service = getattr(self, "psychology", None)
        pattern = service.keyword_pattern() if service is not None else None
        if pattern:
            patterns.append(pattern)
        return patterns

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
