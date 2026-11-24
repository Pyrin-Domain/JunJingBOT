"""转发服务的编排层：识别（detect）→ 分派（record / image）→ 结果落地。

只依赖三样东西：

- MessageProcessor：单条消息的判定与收发工具（``self.mp``）
- NapCatAPI：底层 API 调用（``self.api``，连接由 BotApp 统一创建并共享）
- BotSettings：群号、是否去重、机器人白名单等可调参数

编排层只负责「什么时候调用」：事件进来 → ``ForwardService.forward``，
返回 True 表示这条消息已按搬运逻辑处理完毕，调用方直接 return。

模块分工：

* :mod:`bot.services.forward.job` —— 任务单 / 闸门结论（纯数据）
* :mod:`bot.services.forward.detect` —— 步骤一：监听范围 + 类型识别
* 本模块 —— 步骤二：按类型分派；聊天记录走内容指纹库，图片走独立的送达库
* :mod:`bot.services.dedup` / :mod:`bot.services.image_dedup` —— 两个库，两条数据线
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

from core.logging import get_logger
from messaging.forward_filter import (
    build_forward_nodes,
    clean_forward_messages,
)
from messaging.forward_filter import (
    is_enabled as filter_is_enabled,
)
from messaging.forward_filter import (
    normalize_settings as normalize_filter_settings,
)
from messaging.meme import mface_cq
from messaging.image_filter import FilterHit
from transport.api import OneBotAPIError

from ..dedup import (
    CLAIM_FRESH_SECONDS,
    ForwardDedupStore,
    build_fingerprint,
    clean_group_ids,
    content_identities,
    dedup_log,
    get_multimsg_detail,
    get_multimsg_resid,
    identity_text,
    short_url,
)
from ..image_dedup import (
    ForwardImageStore,
    bytes_identity,
    chat_key,
    perceptual_identity,
)
from .detect import detect_job
from .image_gate import ImageAuditGate
from .job import (
    RATE_LIMIT_RETCODE,
    BatchForwardResult,
    ForwardJob,
    GateOutcome,
    RecoveryBatch,
    RetryTarget,
    SendOutcome,
    StagedForward,
)

__all__ = ["ForwardService"]

log = get_logger("bot.services.forward")

# 重建的聊天记录动辄几十张图，NapCat 要逐张上传，默认 30 秒回执来不及
REBUILT_FORWARD_TIMEOUT = 180.0

#: 限流重试的节奏。QQ 限制「本群每分钟 10 条」，一条一条间隔 6 秒最稳妥；
#: 重试轮数用完还失败就放弃（下次同一内容再出现时会自然补发）。
RETRY_INTERVAL_SECONDS = 6.0
RETRY_MAX_ROUNDS = 10
#: 重试队列上限，防止长时间限流把内存撑爆
RETRY_QUEUE_MAX = 200

#: 算图片指纹时单张图的下载超时（秒）。图片过滤门已经下过的话直接命中它的
#: 字节缓存，不会真的走网络；过滤门关着时才是一次额外下载。
IMAGE_FINGERPRINT_TIMEOUT = 8.0

#: 过滤门摊平聊天记录时的叶子消息上限。去重口径只要 200 条，但过滤要求
#: 「一张都别漏」——实测一张大记录摊平后有 170~300 条，统一放到 1000。
FILTER_SCAN_MAX_ITEMS = 1000

#: ``_relay_nested_forward`` 的三种结果。不用 bool：以前 True 既表示
#: 「已重建发出」，上层无法区分「发出去了」和「按策略不搬」，日志里也看不出跳过。
RELAY_FALLBACK = ""  #: 不用介入，交回整卡搬运
RELAY_SENT = "sent"  #: 已按重建方式发出
RELAY_SKIP = "skip"  #: 按策略不搬（内层卡片 m_resid 为空，内容拿不全）


def message_identities(event) -> tuple[str, ...]:
    """一条消息的**所有**稳定身份，用来判「这条是不是刚搬过」。

    为什么要多个而不是挑一个（实测教训）：同一条消息在不同来源里字段不一样 ——

    - SnowLuma 的**实时事件**没有 ``real_id``，只有 ``message_id``；
    - 断点回放读的是历史接口，``real_id`` 是有的（值等于 ``message_id``）。

    以前按「``real_id`` 优先，否则 ``message_id``」只取**一个**键，于是实时按下标存、
    回放按另一种键查，永远对不上：19:01:36 实时搬过的图，19:02:18 回放又搬了一遍。
    现在两种键都生成，**命中任意一个**就算搬过；两个都没有就不记忆（宁可可能重复，
    也不能凭错误身份误杀）。
    """
    event = event or {}
    keys: list[str] = []
    real_id = event.get("real_id")
    if real_id not in (None, "", 0, "0"):
        keys.append(f"real_id:{real_id}")
    message_id = event.get("message_id")
    if message_id not in (None, "", 0, "0"):
        scope = event.get("group_id") or event.get("user_id") or ""
        keys.append(f"message_id:{scope}:{message_id}")
    return tuple(keys)


def image_content_identities(event) -> list[str]:
    """图片消息的**内容级身份**：按图片文件哈希算身份，跨消息、跨群稳定。

    现有身份 ``message_id:*`` / ``real_id:*`` 只认「这条消息」——同一张图换了
    消息 id（断点回放、从别的群再转），身份就变了，还会被当成新图重复搬运。
    这里取每张普通图片的 ``data.file``，若像 32 位 md5 就按内容哈希收录；
    否则退到 URL 里的 ``fileid``（SnowLuma 的图片 URL 通常带稳定的 fileid）。

    只取普通图片（iter_photos 已排除表情 / 商城表情 / 收藏表情）。
    多图消息按「整条图集合」做签名，避免「旧图+新图」被任意一张命中而误杀新图。
    """
    try:
        from AbstractionLayer.segments import iter_photos
    except Exception:  # pragma: no cover - 抽象层缺失时退化
        return []

    names: list[str] = []
    fileids: list[str] = []
    for seg in iter_photos(event):
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            continue
        file_name = str(data.get("file") or "").strip()
        stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        lowered = stem.lower()
        hex_like = sum(1 for ch in lowered if ch in "0123456789abcdef") >= 16
        if hex_like or len(stem) >= 16:
            names.append(lowered)
            continue
        url = str(data.get("url") or "").strip()
        if url:
            try:
                qs = parse_qs(urlparse(url).query)
            except Exception:
                qs = {}
            fileid = (qs.get("fileid") or [None])[0]
            if fileid:
                fileids.append(str(fileid).lower())

    identities: list[str] = []
    if names:
        joined = "|".join(sorted(set(names)))
        identities.append("image:" + hashlib.blake2b(joined.encode(), digest_size=8).hexdigest())
    if fileids:
        joined = "|".join(sorted(set(fileids)))
        identities.append("image_fileid:" + hashlib.blake2b(joined.encode(), digest_size=8).hexdigest())
    return identities


def _image_hash_api():
    """惰性拿媒体层的图片双哈希接口。

    ``media.image_hash`` 由媒体子代理实现；并行迁移期间可能还不存在，
    这时去重闸门退化成纯文本判定（与旧版 Pillow 缺失时一致），不影响启动。
    """
    try:
        from media.image_hash import IMAGE_HASH_AVAILABLE, hash_remote_images_report
    except Exception:
        return False, None, "image_hash_unavailable"
    return bool(IMAGE_HASH_AVAILABLE), hash_remote_images_report, "pillow_missing"


def _video_hash_api():
    """惰性拿媒体层的视频指纹接口；ffmpeg/Pillow 缺失时安静降级。"""
    try:
        from media.video_hash import hash_remote_videos_report
    except Exception:
        return None, "video_hash_unavailable"
    return hash_remote_videos_report, ""


class ForwardService:
    """自动搬运 / 去重闸门 / 图片批量搬运（不关心事件从哪来、由谁调度）。"""

    def __init__(
        self,
        *,
        mp,
        api=None,
        settings=None,
        media=None,
        dedup=None,
        image_store=None,
        image_gate=None,
        # 旧参数名：兼容旧调用方 / 旧测试
        nc=None,
        config=None,
    ):
        """
        :param mp: MessageProcessor 实例（文本处理层）
        :param api: NapCatAPI 实例（与 BotApp 共享同一条连接）
        :param settings: BotSettings（或行为相同的 dict）
        :param media: MediaService（暂未直接使用，保留给图片相关扩展）
        :param dedup: 现成的 ForwardDedupStore（聊天记录指纹库）；不传则惰性创建
        :param image_store: 现成的 ForwardImageStore（图片送达库，独立 DB）；不传则惰性创建
        :param nc: 旧参数名，等价于 ``api``
        :param config: 旧参数名，等价于 ``settings``
        """
        self.mp = mp
        self.api = api if api is not None else nc
        self.settings = settings if settings is not None else config
        self.media = media
        self._dedup_store: ForwardDedupStore | None = dedup
        self._image_store: ForwardImageStore | None = image_store
        #: 调用方注入的 store（测试用 :memory:）不要被热加载重建掉
        self._image_store_injected = image_store is not None
        # 图片过滤门：配置关着 / settings=None 时直接停用，旧测试不受影响
        self.image_gate = image_gate or ImageAuditGate(
            settings=self.settings, api=self.api, mp=self.mp
        )
        # 旧字段名，兼容按位置/属性访问的调用方
        self.nc = self.api
        self.config = self.settings
        #: 断点恢复的当前批次（见 :meth:`begin_recovery_batch`）；None = 实时路径
        self._recovery_batch: RecoveryBatch | None = None
        #: 限流失败的重试队列（retcode=100）；recover 收尾 / 实时发送后统一重发
        self._retry_queue: list[RetryTarget] = []
        self._retry_task = None

    # ════════════════ 配置读取（BotSettings / dict 都认） ════════════════
    def _get(self, key: str, default=None):
        getter = getattr(self.settings, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:  # 不是 BotSettings 风格的 get
                pass
        if isinstance(self.settings, dict):
            node = self.settings
            for part in str(key).split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node
        return default

    def _reply(self, name: str, default: str = "") -> str:
        reply = getattr(self.settings, "reply", None)
        if callable(reply):
            try:
                return reply(name, default)
            except TypeError:
                return str(reply(name) or default)
        if isinstance(self.settings, dict):
            return str((self.settings.get("replies") or {}).get(name, default))
        return default

    def _img_file(self, name: str) -> str:
        """回复图片的本机路径（``{imgs_dir}`` 由 images 层解析）。"""
        paths = getattr(self.settings, "paths", None)
        if paths is not None:
            try:
                return str(paths.images_dir / name)
            except Exception:
                pass
        return "{imgs_dir}/" + name

    # ════════════════════ 自动搬运 + 去重 ════════════════════
    def _is_self_event(self, event) -> bool:
        """这条事件是不是机器人自己发出去的。

        目标群基本都在来源群列表里（否则群里发的东西搬不出去），所以「搬进目标群」
        的那张卡片会以 ``user_id = 自己`` 的身份再回来一次。这种回流必须当场掐掉：
        指纹对得上时只是白跑一趟（还会在目标群回一句【发过了喵】），对不上时
        （weak 指纹取不到图最常见）就会把同一份内容再搬去剩下的群。

        自己是谁：优先 ``mp.user_id``（登录后就有），老事件没有该字段时退回事件自带的
        ``self_id``。两个都拿不到就**不判**——宁可重复一次，也不误杀别人的消息。
        """
        sender = str((event or {}).get("user_id") or "")
        if not sender:
            return False
        me = str(
            getattr(self.mp, "user_id", "") or (event or {}).get("self_id") or ""
        ).strip()
        return bool(me) and me != "0" and sender == me

    async def forward(
        self,
        event,
        is_recover=False,
        groupid_list=None,
        userid_list=None,
        target_groupid=None,
        task_userid=None,
        dedup=False,
        auto_img_forward_from_bot=None,
        strip_tail_ads=None,
        forward_filter=None,
    ) -> bool:
        """自动搬运的统一入口：识别 → 分派。

        流程只有两步，逻辑各归各的模块：

        1. :func:`bot.services.forward.detect.detect_job` 回答「是不是监听范围」
           「是聊天记录 / 哔哩哔哩卡片 / 图片」「这次发给谁（已剔除消息自己所在群）」；
        2. 按类型分派：

           - 聊天记录 / 卡片 → :meth:`_forward_record`：过内容指纹闸门，闸门返回
             **减完的目标群** + ``extra_reply``（该群发过就回一句【发过了喵】），
             再按剩下的目标发送；
           - 图片（白名单机器人）→ :meth:`_forward_image`：过图片送达库，
             同样按剩下的目标发送，``extra_reply`` 恒为 ``None``。

        :param dedup: 聊天记录是否启用内容去重。图片门不看这个开关——它按「这条
                      消息送达过哪些会话」判，与内容指纹无关。
        :param is_recover: True = 断点回放。闸门照样生效（否则崩溃前最后 10 秒的
                           消息会被回放再搬一次），但**不发【发过了喵】**——那不是
                           群里有人重复发，别往群里刷提示。

        其余参数含义见模块文档 / ``settings.auto_forward``（``forward_filter`` /
        ``strip_tail_ads`` 原样传进来即可）。
        """
        # 菲比暴论这类「先命令、后出图」：命令消息本身不搬运，但要把文本记下来，
        # 3~4 秒后机器人出图的事件才拿得到上下文。
        self.image_gate.note_event(event)
        job = detect_job(
            self,
            event,
            is_recover=is_recover,
            groupid_list=groupid_list,
            userid_list=userid_list,
            target_groupid=target_groupid,
            task_userid=task_userid,
            dedup=dedup,
            auto_img_forward_from_bot=auto_img_forward_from_bot,
            strip_tail_ads=strip_tail_ads,
            forward_filter=forward_filter,
        )
        if job is None:
            return False
        if job.is_image:
            handled = await self._forward_image(job)
        else:
            handled = await self._forward_record(job)
        # 实时路径的限流重试在后台跑（recover 期间由批次收尾统一跑）。
        if self._recovery_batch is None or self._recovery_batch.sealed:
            self._schedule_retry()
        return handled

    # 旧名字：编排层 / 老测试都还在用，内部直接转给 forward()
    async def auto_forward(self, event, **kwargs) -> bool:
        return await self.forward(event, **kwargs)

    # ──────────────────────── 聊天记录 / 卡片 ────────────────────────
    async def _forward_record(self, job: ForwardJob) -> bool:
        """聊天记录搬运：闸门给目标群做减法，剩下的照发，命中该群才回一句。"""
        event = job.event
        if job.blocked:
            return True

        outcome = GateOutcome(verdict="miss", targets=list(job.target_groups))
        batch = self._recovery_batch
        # flush 已经封口但还没清掉批次引用时，新来的实时消息要走实时发送，
        # 不能塞进已经 drain 过的批次里（那样既不会发也不会入库）。
        recovering = batch is not None and not batch.sealed
        if job.dedup:
            outcome = await self.forward_dedup_gate(
                event,
                miniapp_info=job.miniapp_info,
                target_groupid=job.target_groups,
                cur_group_is_target=job.self_group_is_target,
                # recover 期间**不往主库认领**：这一批的认领统一记在 RecoveryBatch
                # （内存临时表）里，发送时才真正入库。否则「预留的目标群」会被
                # 后续事件当成「已经送达」，把目标全减光（见 _send_staged）。
                reserve=not recovering,
            )
        if recovering:
            return self._stage_recovery_record(job, outcome)

        # 回放不提示：那是补历史，不是群里有人重复发
        if outcome.extra_reply and not job.is_recover:
            await self._send_extra_reply(event, outcome.extra_reply)

        if not outcome.forwardable and not job.target_users:
            return True

        forwarded = await self.forward_rebuilt(
            event,
            opts=normalize_filter_settings(job.strip_tail_ads),
            target_groupid=outcome.targets,
            task_userid=job.target_users,
        )
        if forwarded is None:
            # 没启用 / 没内容可裁也没追加 → 还是原来的整卡转发
            forwarded = await self.batch_forward_single_msg(
                event,
                target_groupid=outcome.targets,
                task_userid=job.target_users,
                db_idx=outcome.matched.get("id") if outcome.matched else None,
            )
        sent_groups, sent_users = self._sent_targets(
            forwarded, outcome.targets, job.target_users
        )
        # 真的发出去了才登记指纹，避免“匹配但无处可转”的消息污染指纹库
        if outcome.fingerprint is not None and (sent_groups or sent_users):
            await self.forward_dedup_record(
                outcome.fingerprint, event, target_groupid=sent_groups
            )
        self._enqueue_rate_limited(
            forwarded,
            event,
            groups=outcome.targets,
            users=job.target_users,
            kind="record",
            fingerprint=outcome.fingerprint,
            db_idx=outcome.matched.get("id") if outcome.matched else None,
        )
        return True

    def _stage_recovery_record(
        self, job: ForwardJob, outcome: GateOutcome
    ) -> bool:
        """恢复第一阶段：不写主库，只把结论挂进批次（RecoveryBatch 就是临时记录表）。

        断点恢复是「多个群并发回放同一段时间」：A 群和 B 群可能同时拿到同一份
        聊天记录。闸门在 recover 期间 ``reserve=False``，不会把目标群认领进主库；
        同一份内容靠批次按身份 / 指纹文本合并。等所有群都收完，由
        :meth:`_flush_recovery_batch` 统一算目标（目标并集 − 来源并集 − recover
        前已送达）、统一发送，**发送成功后才真正入库**。
        """
        batch = self._recovery_batch
        assert batch is not None
        fingerprint = outcome.fingerprint or {}
        identities = tuple(
            fingerprint.get("identities") or content_identities(job.event) or []
        )
        source_groups: tuple = ()
        source_users: tuple = ()
        if job.event.get("group_id") is not None:
            source_groups = (str(job.event.get("group_id")),)
        if job.event.get("user_id") is not None:
            source_users = (str(job.event.get("user_id")),)
        staged = StagedForward(
            job=job,
            outcome=outcome,
            identities=identities,
            target_groups=tuple(job.target_groups),
            target_users=tuple(job.target_users),
            source_groups=source_groups,
            source_users=source_users,
            # 只把**主库命中**的群当「已送达」；本批认领的目标群不进这里。
            recorded_groups=tuple((outcome.matched or {}).get("group_ids") or []),
            strip_tail_ads=job.strip_tail_ads,
        )
        if not staged.identities:
            staged.identities = (fingerprint.get("text") or f"event:{id(job.event)}",)
        batch.add(staged)
        return True

    def recovered_pending(self) -> int:
        """批次里还有多少条内容等着第二阶段发送。"""
        batch = self._recovery_batch
        return len(batch) if batch is not None else 0

    def begin_recovery_batch(self) -> RecoveryBatch:
        """开一轮恢复批次；并发触发（启动 + 重连）时复用同一个，最后一个人收尾。

        每调用一次 ``pending`` +1，必须配一次 :meth:`end_recovery_batch`。归零时才
        执行第二阶段（统一算目标 → 发送），所以两个恢复任务交错跑也不会各发一遍。
        """
        batch = self._recovery_batch
        if batch is None or batch.sealed:
            batch = RecoveryBatch()
            self._recovery_batch = batch
        batch.pending += 1
        return batch

    async def end_recovery_batch(self, batch: RecoveryBatch | None = None) -> None:
        """一轮恢复结束：最后一个收尾者做 flush（入库全部完成后才发送）。"""
        batch = batch or self._recovery_batch
        if batch is None:
            return
        batch.pending = max(0, batch.pending - 1)
        if batch.pending:
            return
        if batch.sealed:
            return
        try:
            await self._flush_recovery_batch(batch)
        finally:
            if self._recovery_batch is batch:
                self._recovery_batch = None

    @asynccontextmanager
    async def recovery_batch(self):
        """断点恢复用的事务式批次：出口统一 flush，异常也不会把料丢在内存里。

            async with forward.recovery_batch():
                await pipeline.process_message(event, is_recover=True)  # 多次
        """
        batch = self.begin_recovery_batch()
        try:
            yield batch
        finally:
            await self.end_recovery_batch(batch)

    async def _flush_recovery_batch(self, batch: RecoveryBatch) -> None:
        """第二阶段：所有群都入库完，统一算目标群并发送（每份内容只发一次）。"""
        # 先封口再取料：flush 期间新开一轮恢复会新建批次，不会往这次里塞东西。
        batch.finalize()
        items = batch.drain()
        if items:
            log.info("[断点恢复] 入库完成，开始统一发送（%s 份内容）", len(items))
            for item in items:
                try:
                    await self._send_staged(item)
                except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整批
                    log.warning(
                        "断点恢复发送失败（message_id=%s）: %s",
                        (item.event or {}).get("message_id"),
                        exc,
                    )
        # 限流失败的目标等窗口过去后重发；这是「入库→发送」之后最后一件事，
        # 失败项重发成功后再补登记，避免部分成功被整批丢弃。
        try:
            await self.re_send()
        except Exception as exc:  # noqa: BLE001 - 重试失败不能拖垮收尾
            log.warning("断点恢复限流重试异常: %s", exc)

    async def _send_staged(self, item: StagedForward) -> None:
        """第二阶段：按 ``目标并集 − 来源并集 − recover 前已送达`` 发送这一份内容。

        为什么在发送前才做减法：同一份内容可能被 A、B 两个来源群各收一次料，
        第二阶段合并成一份。两条合并后的正确结果就是::

            来源 = 来源1 ∪ 来源2
            目标 = 目标1 ∪ 目标2 − 来源

        来源群本来就有这份内容，不能再发回去；而本批认领时预留的目标群**不是**
        来源，不能拿来减（否则目标会被减光，整份内容一个群都发不出去）。
        """
        job = item.job
        event = job.event
        recorded = {str(g) for g in item.recorded_groups}
        sources = {str(g) for g in item.source_groups}
        targets = [
            g
            for g in dict.fromkeys(item.target_groups)
            if str(g) not in recorded and str(g) not in sources
        ]
        source_users = {str(u) for u in item.source_users}
        users = [
            u
            for u in dict.fromkeys(item.target_users)
            if str(u) not in source_users and str(u) != str(event.get("user_id"))
        ]

        sent_groups: list = []
        sent_users: list = []
        if targets or users:
            forwarded = await self.forward_rebuilt(
                event,
                opts=normalize_filter_settings(item.strip_tail_ads),
                target_groupid=targets,
                task_userid=users,
            )
            if forwarded is None:
                forwarded = await self.batch_forward_single_msg(
                    event,
                    target_groupid=targets,
                    task_userid=users,
                    db_idx=item.outcome.matched.get("id")
                    if item.outcome.matched
                    else None,
                )
            sent_groups, sent_users = self._sent_targets(forwarded, targets, users)
            self._enqueue_rate_limited(
                forwarded,
                event,
                groups=targets,
                users=users,
                kind="record",
                fingerprint=item.outcome.fingerprint,
                db_idx=item.outcome.matched.get("id") if item.outcome.matched else None,
            )
        else:
            log.info(
                "断点恢复：内容 %s 的目标群已全部覆盖，只登记来源群",
                event.get("message_id"),
            )

        if item.outcome.fingerprint is not None:
            # 发送成功的目标 + 来源群 + recover 前已送达的群一起入库；来源群即使
            # 一个都没发也要登记，否则下次回放又会把同一份内容当成新的。
            await self.forward_dedup_record(
                item.outcome.fingerprint,
                event,
                target_groupid=list(sent_groups) + list(item.recorded_groups),
                source_groups=list(item.source_groups) or None,
            )

    async def _send_extra_reply(self, event, summary: str) -> None:
        """闸门要求的额外回应：发一个商城表情（mface）。

        原来发的是本地 ``imgs/news.gif`` + 一句「[发过了喵]」的文字说明。现在
        改成一个商城表情：表情自带表情名（``summary`` =「此条news已被其他群友发布过」），
        一只猫就把话说清楚了，不用再配文字。

        表情没配 / 配坏了 → 退回老的本地图片。这条提示是「别重复发」的告知，
        静默丢掉的代价比发一张旧图大。
        """
        mface = self._forwarded_already_mface()
        if mface:
            await self.mp.send_message(event, mface_cq(mface))
            return
        await self.mp.send_img(
            event=event,
            img_addr=self._img_file("news.gif"),
            summary=summary,
        )

    def _forwarded_already_mface(self) -> dict:
        """读「这条发过了」提示用的商城表情配置。

        配置在 ``auto_forward.forwarded_already_mface``：QQ 商城表情要三件套才发得
        出去 —— ``emoji_id``（表情本体）+ ``emoji_package_id``（表情包）+ ``key``
        （包内密钥），``summary`` 是表情名（客户端就显示它）。

        少任何一件都发不出商城表情（会退化成普通图片或者直接失败），所以这里
        按「整套齐了才算数」处理，缺件就返回空 dict，让调用方退回本地图片。
        """
        raw = self._get("auto_forward.forwarded_already_mface", None)
        if not isinstance(raw, dict):
            return {}
        emoji_id = str(raw.get("emoji_id") or "").strip()
        key = str(raw.get("key") or "").strip()
        if not emoji_id or not key:
            return {}
        mface = {"emoji_id": emoji_id, "key": key}
        package = raw.get("emoji_package_id")
        if package not in (None, ""):
            mface["emoji_package_id"] = package
        summary = str(raw.get("summary") or "").strip()
        if summary:
            mface["summary"] = summary
        return mface

    # ──────────────────────── 图片（白名单机器人） ────────────────────────
    async def _photo_fingerprints(
        self, event, *, timeout: float | None = None
    ) -> tuple[list[dict], dict]:
        """给消息里每张普通图片算「真实字节身份 + 感知指纹」。

        为什么要按图片本体算（线上实测）：图片身份 ``image:*`` 取的是 ``data.file``
        的哈希，而 QQ 把同一张图重新上传/重新编码后文件名（md5）就变了 ——

        * 2026-11-06 08:41 群里 5 张截图（md5 全大写）搬了一轮；
        * 14:57 同一组 5 张图换了一组小写 md5 再发，字节不同、像素完全相同，
          ``image:*`` 认不出来 → 又搬了一轮。

        所以对每张图再算两条**内容级**身份：

        * ``imgsha:<blake2b(bytes)>`` —— 字节完全一致（文件名换了也认得出）；
        * ``phash:<aHash+dHash>`` —— 像素级指纹，重新压缩/缩放后仍能模糊匹配。

        返回 ``([{sha, phash}, ...], 诊断报告)``，和 ``iter_photos`` 一一对应。
        取不到字节的图两项都是空串，调用方据此**放弃模糊判定**（宁可多搬一次，
        也不能凭半张图误杀）。
        """
        try:
            from AbstractionLayer.segments import iter_photos
            from media.image_hash import IMAGE_HASH_AVAILABLE
        except Exception:  # pragma: no cover - 抽象层/媒体层缺失时退化
            return [], {"error": "image_hash_unavailable"}
        photos = list(iter_photos(event))
        if not photos:
            return [], {"error": "no_photo"}
        if not IMAGE_HASH_AVAILABLE:
            return [], {"error": "pillow_missing", "photos": len(photos)}

        urls = [
            str((seg.get("data") or {}).get("url") or "").strip() for seg in photos
        ]
        wait = IMAGE_FINGERPRINT_TIMEOUT if timeout is None else float(timeout)
        payloads = await asyncio.gather(
            *(self._load_photo_bytes(url, wait) for url in urls)
        )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._hash_photo_bytes, payloads)

    async def _load_photo_bytes(self, url: str, timeout: float) -> bytes | None:
        """借图片过滤门的下载缓存拿字节；拿不到就返回 None（不额外重试）。"""
        if not url.startswith(("http://", "https://")):
            return None
        loader = getattr(self.image_gate, "load_image", None)
        if not callable(loader):
            return None
        try:
            return await asyncio.to_thread(loader, url, timeout)
        except Exception as exc:  # 单张图失败不能把搬运打挂
            log.debug("图片指纹下载失败 %s: %s", short_url(url), exc)
            return None

    @staticmethod
    def _hash_photo_bytes(payloads) -> tuple[list[dict], dict]:
        """纯 CPU：字节 → (内容身份, 感知指纹)。放工作线程里算。"""
        from media.image_hash import image_hash

        prints: list[dict] = []
        report = {"photos": len(payloads), "hashed": 0}
        for data in payloads:
            item = {"sha": "", "phash": ""}
            if data:
                item["sha"] = bytes_identity(data)
                value = image_hash(data)
                if value:
                    item["phash"] = perceptual_identity(value)
            if item["phash"]:
                report["hashed"] += 1
            prints.append(item)
        return prints, report

    async def _filter_by_image_content(
        self, event, store, identities, groups, users
    ) -> tuple[list[str], list, list]:
        """按图片**本体**再减一轮目标：字节身份（精确）+ 感知指纹（模糊）。

        判定规则：

        * ``imgsha:*`` 精确命中 → 这个会话送过**字节完全相同**的图；
        * ``phash:*`` 模糊命中 → 这个会话送过同一张图（重新压缩 / 缩放 / 换文件名
          都算）。但必须**这条消息里的每张图都命中**才跳过 —— 多图消息里
          「旧图 + 新图」要照常搬，不能因为一张旧图把新图吞掉。

        取不到图片（没有 url / 下载失败 / 算不出哈希）时原样返回，宁可多搬一次，
        也不凭半张图误杀。
        """
        prints, report = await self._photo_fingerprints(
            event, timeout=store.perceptual_timeout
        )
        if not prints:
            log.debug(
                "机器人图片 %s 算不出图片指纹（%s），只按消息身份判",
                event.get("message_id"),
                report.get("error") or report,
            )
            return identities, groups, users
        extra = [item["sha"] for item in prints if item.get("sha")]
        phashes = [item["phash"] for item in prints if item.get("phash")]
        identities = list(identities) + extra + phashes
        if not extra and not phashes:
            return identities, groups, users

        chats = [chat_key(group_id=g) for g in groups] + [
            chat_key(user_id=u) for u in users
        ]
        delivered: set[str] = set()
        if extra and len(extra) == len(prints):
            # 字节身份也要「每张图都在这个会话送过」才算 —— 多图消息里
            # 「旧图 + 新图」时，只凭一张旧图命中就把整条跳过会吞掉新图。
            per_photo = await asyncio.gather(
                *(store.delivered_async([sha], chats) for sha in extra)
            )
            covered = set(chats)
            for got in per_photo:
                covered &= got
            delivered |= covered
        if phashes and len(phashes) == len(prints):
            matched = await store.matched_phash_async(
                chats, phashes, store.perceptual_max_distance
            )
            for chat in chats:
                # 每张图都有近邻才算「这条消息送过」
                if len(matched.get(chat) or ()) >= len(phashes):
                    delivered.add(chat)

        remaining_groups = [
            g for g in groups if chat_key(group_id=g) not in delivered
        ]
        remaining_users = [
            u for u in users if chat_key(user_id=u) not in delivered
        ]
        skipped = len(groups) + len(users) - len(remaining_groups) - len(remaining_users)
        if skipped:
            log.info(
                "机器人图片 %s 的图片内容已送过 %s 个会话（指纹 %s/%s 张），只补没送过的",
                event.get("message_id"),
                skipped,
                len(phashes),
                len(prints),
            )
        return identities, remaining_groups, remaining_users

    async def _forward_image(self, job: ForwardJob) -> bool:
        """图片搬运：按「消息身份 + 图片内容身份 × 会话」查送达库，只补没送过的会话。

        没有 ``extra_reply``：图片没有正文，重复与否用户看不出来，回一句
        【发过了喵】只会让人莫名其妙。做的是「别重复发」而不是「告诉别人重复了」。

        身份分三层（命中任意一层就算搬过，见 ``_filter_by_image_content``）：
        消息 id / ``image:*``（文件名哈希）/ ``imgsha:*`` + ``phash:*``（图片本体）。
        最后一层专门对付「同一张图被 QQ 重新编码后 md5 变了」的重复搬运。
        """
        event = job.event
        # 过滤门先于送达库：命中就不要再搬给普通目标，命中证据只发审计群。
        hits = await self.image_gate.screen_event(event)
        if hits:
            try:
                await self.image_gate.publish_event(event, hits)
            except Exception as exc:  # 打标签失败不能反过来把流程打挂
                log.warning("单图过滤标签发送异常: %s", exc)
            if bool(self.image_gate.config.get("block_matched", True)) and any(
                not hit.manual for hit in hits
            ):
                # manual 命中（黑白图待人工）只审计不拦；有色图命中才拦
                log.info(
                    "机器人图片 %s 命中 %s，不搬给普通目标",
                    event.get("message_id"),
                    ", ".join(hit.label for hit in hits),
                )
                return True

        store = self._get_image_store()
        identities: list[str] = []
        groups = list(job.target_groups)
        users = list(job.target_users)
        if store.enable:
            identities = list(message_identities(event)) + image_content_identities(event)
            chats = [chat_key(group_id=g) for g in groups]
            chats += [chat_key(user_id=u) for u in users]
            delivered = (
                await store.delivered_async(identities, chats) if identities else set()
            )
            groups = [g for g in groups if chat_key(group_id=g) not in delivered]
            users = [u for u in users if chat_key(user_id=u) not in delivered]
            # 消息身份 / 文件名哈希都没命中时，再按「图片本体」判一次：
            # 同一张图被重新编码（文件名 md5 变了）只有感知指纹认得出来。
            if (groups or users) and store.perceptual:
                identities, groups, users = await self._filter_by_image_content(
                    event, store, identities, groups, users
                )
        else:
            log.info(
                "机器人图片查重已关闭，%s 直接搬运", event.get("message_id")
            )

        if not groups and not users:
            log.info(
                "机器人图片 %s 已送往全部目标，跳过（断点回放重复 / 已送达）",
                event.get("message_id"),
            )
            return True

        scheduled = await self.batch_forward_single_msg(
            event, target_groupid=groups, task_userid=users
        )
        attempted = len(groups) + len(users)
        sent_groups, sent_users = self._sent_targets(scheduled, groups, users)
        # 成功的目标立刻入库（不再因为另一个目标失败而整批不记）；
        # 失败的目标不登记，等下次重复 / 限流重试成功时再补。
        if identities and (sent_groups or sent_users):
            await store.remember_async(
                identities,
                [chat_key(group_id=g) for g in sent_groups]
                + [chat_key(user_id=u) for u in sent_users],
            )
        self._enqueue_rate_limited(
            scheduled,
            event,
            groups=groups,
            users=users,
            kind="image",
            identities=tuple(identities),
        )
        if isinstance(scheduled, BatchForwardResult) and scheduled.failed_id:
            rate_limited = len(scheduled.rate_limited)
            other = len(scheduled.failed_id) - rate_limited
            if rate_limited:
                log.warning(
                    "机器人图片 %s 有 %s/%s 个目标被限流，已入队等待重发"
                    "（成功 %s 个已登记）",
                    event.get("message_id"),
                    rate_limited,
                    attempted,
                    len(sent_groups) + len(sent_users),
                )
            if other:
                log.warning(
                    "机器人图片 %s 有 %s/%s 个目标发送失败（非限流），暂不登记，"
                    "下次重复时补发",
                    event.get("message_id"),
                    other,
                    attempted,
                )
        return True

    def _get_image_store(self) -> ForwardImageStore:
        """惰性创建图片送达库（独立 DB；不搬图片就不会建文件）。"""
        if self._image_store is None:
            self._image_store = ForwardImageStore(settings=self.settings)
        return self._image_store

    def reload_image_dedup(self) -> None:
        """配置热加载后丢掉旧的图片送达库，下次用到时按新配置重建。

        保留调用方注入的 store（测试里的 ``:memory:`` 不能换掉）。
        """
        if self._image_store_injected or self._image_store is None:
            return
        self._image_store.close()
        self._image_store = None

    # ════════════ 末尾广告裁剪 + 尾部追加（重建成新的聊天记录） ════════════
    async def forward_rebuilt(
        self,
        event,
        *,
        opts: dict | None = None,
        target_groupid=None,
        task_userid=None,
    ) -> int | None:
        """取回转发内容 → 掐掉末尾广告 → 追加自定义消息 → 重建成新聊天记录发出。

        为什么不能直接整卡搬：``forward_group_single_msg`` 只能把原来那张卡片
        原样转一次，内容动不了。想删末尾的二维码海报 / 群推荐卡片，只能把每条
        记录重建成 node 再 ``send_group_forward_msg``。

        :return: 成功发送的目标数量；**None 表示「不用重建」**，调用方退回整卡转发
                 （没开功能、取不到内容、一条没丢也没追加、或者全部发送失败）
        """
        target_groupid = list(target_groupid or [])
        task_userid = list(task_userid or [])
        # opts 容忍 None（只开图片扫描、没配裁剪的调用方）：
        # 注意不能重新 normalize —— 已经是 {} 的「功能关闭」会被补成默认开启
        if opts is None:
            opts = {}
        tail_enabled = filter_is_enabled(opts)
        scan_images = bool(
            self.image_gate.enabled
            and self.image_gate.config.get("scan_all_images", True)
        )
        if not tail_enabled and not scan_images:
            return None
        kind = self._forward_kind(event)
        if kind not in ("old_forward", "multimsg"):
            return None  # 小程序卡片没有「末尾几条」的概念，保持原样搬运

        messages, fetched = await self._fetch_forward_messages(event, kind)
        if not messages:
            log.warning("取不到转发内容（%s），照常整卡转发", fetched)
            return None

        # 下载图片 + 解码二维码是同步阻塞的，扔到线程里别卡住事件循环。
        # image_loader 走过滤门的缓存：过滤刚下过的图，二维码 / 审计标签直接复用。
        nodes, report = await asyncio.to_thread(
            build_forward_nodes,
            messages,
            opts,
            image_loader=self.image_gate.load_image,
        )

        if scan_images:
            hits = await self._screen_record_images(event, kind, messages)
            if hits:
                try:
                    await self.image_gate.publish_record(event, hits, nodes=nodes)
                except Exception as exc:  # 打标签失败不能反过来把流程打挂
                    log.warning("聊天记录过滤标签发送异常: %s", exc)
                if bool(self.image_gate.config.get("block_matched", True)) and any(
                    not hit.manual for hit in hits
                ):
                    labels = ", ".join(sorted({hit.label for hit in hits}))
                    log.info(
                        "聊天记录 %s 命中 %s，不搬给普通目标",
                        event.get("message_id"),
                        labels,
                    )
                    return 0

        if not tail_enabled or not report.get("rebuilt"):
            return None

        removed = report.get("removed") or []
        removed_desc = ", ".join(f"#{item['index']}:{item['reason']}" for item in removed) or "无"
        log.info(
            "转发裁剪：共 %s 条，丢弃 %s 条（%s），追加 %s 条，重建 %s 个节点",
            report["total"],
            len(removed),
            removed_desc,
            report.get("appended", 0),
            len(nodes),
        )
        dedup_log(
            "filter",
            event,
            kind=kind,
            total=report["total"],
            kept=report.get("kept"),
            appended=report.get("appended", 0),
            removed=removed,
            fwd=fetched,
        )

        sent = 0
        attempts = 0
        unknown = 0  # 超时这类「不知道到底发没发出去」的结果
        for group_id in target_groupid:
            if str(group_id) == str(event.get("group_id")):
                continue
            attempts += 1
            res = await self._send_rebuilt(group_id=group_id, nodes=nodes)
            if res is True:
                sent += 1
            elif res is None:
                unknown += 1
        for user_id in task_userid:
            if str(user_id) == str(event.get("user_id")):
                continue
            attempts += 1
            res = await self._send_rebuilt(user_id=user_id, nodes=nodes)
            if res is True:
                sent += 1
            elif res is None:
                unknown += 1
        if unknown:
            # 超时时消息往往**已经发出去了**（NapCat 只是回执慢）。这时候如果再走
            # 整卡转发，群里就会出现两张卡片（线上实测过一次）。所以只承认失败、
            # 不承认「没回执」，宁可少发一张卡也不要重复搬运。
            log.warning(
                "%s 个重建目标没有回执，按已发出处理，不退回整卡转发",
                unknown,
            )
            return sent + unknown
        if attempts and not sent:
            log.warning("重建后的消息一条都没发出去，退回整卡转发")
            return None
        return sent

    async def _send_rebuilt(self, *, group_id=None, user_id=None, nodes=None) -> bool | None:
        """发送重建好的聊天记录。

        :return: True = 成功；False = 明确失败（status=failed，可以安全退回整卡转发）；
                 None = 没回执（超时 / 连接异常），**不确定发没发出去**
        """
        label = f"群 {group_id}" if group_id is not None else f"好友 {user_id}"
        try:
            await self.mp.ensure_connected()
            if group_id is not None:
                res = await self.api.send_group_forward_msg(
                    group_id=group_id, messages=nodes, timeout=REBUILT_FORWARD_TIMEOUT
                )
            else:
                res = await self.api.send_private_forward_msg(
                    user_id=user_id, messages=nodes, timeout=REBUILT_FORWARD_TIMEOUT
                )
        except TimeoutError:
            log.warning("发送到%s超时（%g 秒没回执）", label, REBUILT_FORWARD_TIMEOUT)
            return None
        except OneBotAPIError as exc:
            # 后端明确拒绝（retcode != 0）：**确定没发出去**，可以安全退回整卡转发。
            # 注意这跟超时不同，超时时发没发出去是不确定的。
            log.warning("发送到%s被拒: %s", label, exc)
            return False
        except Exception as exc:
            log.warning("发送到%s异常: %s", label, exc)
            return None
        status = str((res or {}).get("status") or "")
        if status == "failed":
            log.warning("发送到%s失败: %s", label, (res or {}).get("message"))
            return False
        return True

    # ══════════ 机器人发图自动搬运：白名单匹配（type = group / private / all） ══════════
    @staticmethod
    def bot_whitelist_entries(auto_img_forward_from_bot: dict | None) -> list[dict]:
        """把配置里的「视为机器人的 QQ」统一成对象列表。

        新写法 bot_list —— 每个元素是一个对象，可分别限定生效范围：

            {"bot_id": 10005, "type": "group", "group_id": [], "user_id_list": []}

            type          group = 只看群聊；private = 只看私聊；all = 群聊与私聊都看
            group_id      type 含群聊时生效，留空 = 所有群聊
            user_id_list  type 含私聊时生效，留空 = 所有私聊。私聊的发信人就是这个机器人
                          自己，没什么可筛，所以面板里不显示这个字段（手写配置仍然认）

        旧写法 bot_id_list —— [QQ 号, ...]，等价于每个都是 {"bot_id": x, "type": "all"}，
        所以老配置不改也能照常跑。
        """
        cfg = auto_img_forward_from_bot or {}
        entries: list[dict] = []
        raw = cfg.get("bot_list")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    entries.append(item)
                elif str(item).strip():
                    entries.append({"bot_id": item})  # 列表里混了裸 QQ 号也认
        if not entries:
            for item in cfg.get("bot_id_list") or []:
                if str(item).strip():
                    entries.append({"bot_id": item})
        return entries

    @classmethod
    def bot_entry_matches(cls, event, entry: dict) -> bool:
        """一条消息是否命中某个机器人白名单条目。

        先比发消息的人是不是这个机器人，再看范围：
        群聊消息看 event['group_id']，私聊消息看 event['user_id']，
        对应的列表留空 = 该类消息全放行。
        """
        bot_id = str((entry or {}).get("bot_id", "")).strip()
        if not bot_id or str(event.get("user_id", "")).strip() != bot_id:
            return False
        # 事件里通常写 message_type，个别场合写 type，两个都认
        msg_type = event.get("message_type") or event.get("type")
        scope = str((entry or {}).get("type", "all") or "all").strip().lower()
        if msg_type == "group":
            if scope == "private":
                return False
            allow = clean_group_ids(entry.get("group_id"))
            return not allow or str(event.get("group_id")) in allow
        if msg_type == "private":
            if scope == "group":
                return False
            allow = clean_group_ids(entry.get("user_id_list"))
            return not allow or str(event.get("user_id")) in allow
        return False

    @classmethod
    def _is_whitelisted_bot(cls, event, auto_img_forward_from_bot: dict | None) -> bool:
        """只要有一条白名单条目命中就算（列表为空 = 没有机器人）。"""
        return any(
            cls.bot_entry_matches(event, entry)
            for entry in cls.bot_whitelist_entries(auto_img_forward_from_bot)
        )

    # ════════════════ 机器人图片：同一条别搬两次 ════════════════
    # 判定与记录都在 :mod:`bot.services.image_dedup`（独立 DB、按「身份 × 会话」），
    # 这里只留一个方法名兼容旧调用方。
    @staticmethod
    def _message_identities(event) -> tuple[str, ...]:
        """兼容旧名字，实现在模块级 :func:`message_identities`。"""
        return message_identities(event)

    # ══════════ MessageProcessor 的卡片判定（新 MessageProcessor 保留同名方法） ══════════
    def _check_is_miniapp(self, event) -> dict:
        checker = getattr(self.mp, "check_is_miniapp", None)
        if callable(checker):
            return checker(event) or {}
        from ..dedup import get_miniapp_info

        return get_miniapp_info(event)

    def _check_is_old_forward(self, event) -> bool:
        checker = getattr(self.mp, "check_is_old_forward", None)
        if callable(checker):
            return bool(checker(event))
        return any(
            isinstance(seg, dict) and seg.get("type") == "forward"
            for seg in event.get("message", []) or []
        )

    def _check_is_multimsg_forward(self, event):
        checker = getattr(self.mp, "check_is_multimsg_forward", None)
        if callable(checker):
            return checker(event)
        return get_multimsg_resid(event)

    async def forward_single_msg(
        self, event, message_type=None, group_id=None, user_id=None
    ) -> SendOutcome:
        DEBUG = False
        if message_type == "group" and group_id is not None:
            await self.mp.ensure_connected()
            # 跨群搬「聊天记录套聊天记录」时，部分后端会把内层压成 XML
            # （SnowLuma 就是这样），接收方只看到「[XML消息]」打不开。
            # 这种情况改走重建；内层 m_resid 为空（内容压根拿不到）则直接不搬。
            # RELAY_SENT / RELAY_SKIP 都算处理完，只有 RELAY_FALLBACK 继续整卡搬。
            if await self._relay_nested_forward(event, group_id=group_id):
                return SendOutcome(ok=True)
            try:
                await self.api.forward_group_single_msg(
                    group_id=group_id, message_id=event["message_id"]
                )
            except OneBotAPIError as exc:
                # retcode=100 = 本群每分钟只能发 N 条：确定没发出去，可以重试
                log.warning("群 %s 单条转发失败: %s", group_id, exc)
                return SendOutcome(ok=False, retcode=exc.retcode, detail=str(exc))
            except Exception as exc:  # noqa: BLE001 - 单条转发失败不该中断搬运流程
                log.warning("群 %s 单条转发失败: %s", group_id, exc)
                return SendOutcome(ok=False, retcode=None, detail=str(exc))
            refresher = getattr(self.mp, "check_point_active_get", None)
            if callable(refresher):
                await refresher(group_id)
            return SendOutcome(ok=True)
        elif message_type == "private" and user_id is not None:
            await self.mp.ensure_connected()
            # 同上：RELAY_SENT / RELAY_SKIP 都不再走整卡搬运。
            if await self._relay_nested_forward(event, user_id=user_id):
                return SendOutcome(ok=True)
            try:
                await self.api.forward_friend_single_msg(
                    user_id=user_id, message_id=event["message_id"]
                )
            except OneBotAPIError as exc:
                log.warning("好友 %s 单条转发失败: %s", user_id, exc)
                return SendOutcome(ok=False, retcode=exc.retcode, detail=str(exc))
            except Exception as exc:  # noqa: BLE001 - 同上
                log.warning("好友 %s 单条转发失败: %s", user_id, exc)
                return SendOutcome(ok=False, retcode=None, detail=str(exc))
            return SendOutcome(ok=True)
        return SendOutcome(ok=False, retcode=None, detail="invalid_target")

    async def _relay_nested_forward(self, event, *, group_id=None, user_id=None) -> str:
        """跨会话搬卡片时，只在**必要**时改走「重建」，否则交回整卡搬运。

        背景（2026-10-26 实测 + 上游 issue #437）：

        - SnowLuma ≤ 1.14.15 的 ``forward_*_single_msg`` 是「原样重发缓存里那条
          消息」，把源会话的 resid 塞进目标会话，结果是空壳卡（桌面端打不开、
          手机端能进但图片加载失败）。上游提交 ``201d10c`` 改成「展开内层树 →
          对目标会话重新 upload 拿新 resid → 用新 resid 发」，**2.2.0 起已生效**，
          我们跑的正是 2.2.0，所以这类卡片整卡搬就能打开，不必介入。
        - 仍然救不回来的只有一种：内层已经是 ``xml/viewMultiMsg`` 死卡片
          （``m_fileName`` 指向源会话临时文件，入站时就已经降级）。
          此时任何后端整卡重发都只是再发一次坏卡；重建也只能把 ``<title>``
          摘要伪装成一条正常记录，所以按 ``m_resid`` 分两种：
          **为空 → 不搬**（``RELAY_SKIP``），**非空 → 重建**（摘要降级，有损）。

        :return: :data:`RELAY_SENT` / :data:`RELAY_SKIP` / :data:`RELAY_FALLBACK`
                 （空串，表示交回整卡搬运）。
        """
        target = group_id if group_id is not None else user_id
        if target is None:
            return RELAY_FALLBACK
        # 本群搬本群：缓存有效，整卡搬运最保真，不动。
        origin = event.get("group_id") if group_id is not None else event.get("user_id")
        if origin is not None and str(origin) == str(target):
            return RELAY_FALLBACK

        plan_fn = getattr(self.api, "relay_plan", None)
        nodes_fn = getattr(self.api, "relay_nodes", None)
        if not callable(plan_fn) or not callable(nodes_fn):
            return RELAY_FALLBACK

        kind = self._forward_kind(event)
        if kind not in ("old_forward", "multimsg"):
            return RELAY_FALLBACK  # 小程序卡片没有嵌套聊天记录的概念

        messages, fetched = await self._fetch_forward_messages(event, kind)
        if not messages:
            log.warning("取不到转发内容（%s），照常整卡转发", fetched)
            return RELAY_FALLBACK

        try:
            plan = await plan_fn(messages)
        except Exception as exc:  # noqa: BLE001 - 判定失败就退回整卡搬运
            log.warning("嵌套转发方案判定失败（%s），照常整卡转发", exc)
            return RELAY_FALLBACK
        if plan.skip:
            log.info("不搬这张卡片：%s（目标 %s）", plan.reason, target)
            return RELAY_SKIP
        if not plan.needs_rebuild:
            return RELAY_FALLBACK

        log.info("%s，改用重建发送", plan.reason)
        content = await nodes_fn(messages=messages)
        if not content.nodes:
            log.warning(
                "重建失败（%s），照常整卡转发",
                "；".join(content.errors) or "没有节点",
            )
            return RELAY_FALLBACK
        if content.depth_reached:
            log.info(
                "已内联 %s 层嵌套，共 %s 个节点",
                content.depth_reached,
                len(content.nodes),
            )

        if group_id is not None:
            res = await self._send_rebuilt(group_id=group_id, nodes=content.nodes)
        else:
            res = await self._send_rebuilt(user_id=user_id, nodes=content.nodes)
        if res is False:
            log.warning("重建发送失败，退回整卡转发")
            return RELAY_FALLBACK
        return RELAY_SENT

    async def batch_forward_single_msg(
        self,
        event,
        target_groupid=None,
        task_userid=None,
        db_idx: int | None = None,
    ) -> BatchForwardResult:
        """并发把一条消息转发到多个目标，返回**逐目标**结果。

        :param db_idx: 库内主键（聊天记录指纹行 id）。限流重试成功后按它回写，
            不用重新查库；图片送达库的主键是 (identity, chat)，这里不传。
        :return: :class:`BatchForwardResult`；``succeed_id`` / ``failed_id`` 是
            本次实际成功 / 失败的目标，``retcodes`` 里 100 表示限流可重试。
        """
        target_groupid = target_groupid or []
        task_userid = task_userid or []
        sends = []
        labels = []
        targets = []
        for group_id in target_groupid:
            # 比字符串：面板存的是 "1054955587"，事件里是 1054955587，
            # 直接用 == 会把「来源群本身也是目标群」漏掉，结果往消息自己所在的群再发一份。
            if str(group_id) == str(event.get("group_id")):
                continue
            sends.append(
                self.forward_single_msg(
                    event=event, group_id=group_id, message_type="group"
                )
            )
            labels.append(f"群 {group_id}")
            targets.append(("group", group_id))
        for user_id in task_userid:
            if str(user_id) == str(event.get("user_id")):
                continue
            sends.append(
                self.forward_single_msg(
                    event=event, user_id=user_id, message_type="private"
                )
            )
            labels.append(f"好友 {user_id}")
            targets.append(("private", user_id))
        result = BatchForwardResult(attempted=len(sends), db_idx=db_idx)
        if not sends:
            return result
        results = await asyncio.gather(*sends, return_exceptions=True)
        for (message_type, target_id), label, outcome in zip(
            targets, labels, results, strict=False
        ):
            if isinstance(outcome, BaseException):
                log.warning("%s 搬运异常: %s", label, outcome)
                result.failed_id.append(target_id)
                result.retcodes[str(target_id)] = None
                continue
            # 兼容旧调用方 / 测试：bool / None / SendOutcome 都认
            if isinstance(outcome, SendOutcome):
                ok, retcode = outcome.ok, outcome.retcode
            else:
                ok, retcode = bool(outcome), None
            if ok:
                result.succeed_id.append(target_id)
            else:
                result.failed_id.append(target_id)
                result.retcodes[str(target_id)] = retcode
        return result

    # ════════════════════ 限流重试队列（retcode=100） ════════════════════
    @staticmethod
    def _sent_targets(forwarded, groups, users) -> tuple[list, list]:
        """从 ``BatchForwardResult`` / 旧的 int 返回值里取出「真正发成功的目标」。

        - ``BatchForwardResult``：按 ``succeed_id`` 过滤；
        - int（``forward_rebuilt`` 的旧返回）：>0 当作全部目标成功，0/None 当作全失败
          （重建那条链路拿不到逐目标结果，只能这样保守处理）。
        """
        if isinstance(forwarded, BatchForwardResult):
            ok = {str(x) for x in forwarded.succeed_id}
            return (
                [g for g in groups if str(g) in ok],
                [u for u in users if str(u) in ok],
            )
        if forwarded:
            return list(groups), list(users)
        return [], []

    def _enqueue_rate_limited(
        self,
        forwarded,
        event,
        *,
        groups=(),
        users=(),
        kind: str,
        identities: tuple = (),
        fingerprint: dict | None = None,
        db_idx: int | None = None,
    ) -> None:
        """把 retcode=100 的失败目标放进重试队列（其他失败不重试）。"""
        if not isinstance(forwarded, BatchForwardResult):
            return
        if db_idx is None:
            db_idx = forwarded.db_idx
        group_set = {str(g) for g in groups}
        user_set = {str(u) for u in users}
        for target_id in forwarded.rate_limited:
            key = str(target_id)
            message_type = "group" if key in group_set else "private"
            if message_type == "private" and key not in user_set:
                message_type = "group"
            self._enqueue_retry(
                RetryTarget(
                    event=event,
                    message_type=message_type,
                    target_id=target_id,
                    kind=kind,
                    identities=tuple(identities),
                    fingerprint=fingerprint,
                    db_idx=db_idx,
                    retcode=RATE_LIMIT_RETCODE,
                )
            )

    def _enqueue_retry(self, item: RetryTarget) -> None:
        """入队（同一条消息 + 同一目标只留一份；满了丢最旧的）。"""
        for existing in self._retry_queue:
            if (
                existing.kind == item.kind
                and str(existing.target_id) == str(item.target_id)
                and existing.event.get("message_id") == item.event.get("message_id")
                and existing.event.get("group_id") == item.event.get("group_id")
            ):
                existing.retcode = item.retcode
                return
        if len(self._retry_queue) >= RETRY_QUEUE_MAX:
            log.warning("限流重试队列已满（%s），丢弃最旧的一条", RETRY_QUEUE_MAX)
            self._retry_queue.pop(0)
        self._retry_queue.append(item)

    def _schedule_retry(self) -> None:
        """实时路径：有重试项就起一个后台任务，不阻塞当前消息处理。"""
        if not self._retry_queue:
            return
        if self._retry_task is not None and not self._retry_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环（同步测试）就等下一次
            return
        self._retry_task = loop.create_task(self.re_send(), name="forward-retry")

    async def re_send(
        self, *, interval: float | None = None, max_rounds: int | None = None
    ) -> int:
        """把限流队列里的目标重发一遍，成功一个就回写一个。

        限流是「每分钟 10 条」，所以每条之间默认间隔 ``RETRY_INTERVAL_SECONDS``
        秒；一轮发完还有失败的就再等一轮，最多 ``RETRY_MAX_ROUNDS`` 轮。

        :return: 本次重发成功的条数
        """
        interval = RETRY_INTERVAL_SECONDS if interval is None else float(interval)
        max_rounds = RETRY_MAX_ROUNDS if max_rounds is None else int(max_rounds)
        if not self._retry_queue:
            return 0
        sent = 0
        for round_no in range(max(1, max_rounds)):
            pending = list(self._retry_queue)
            self._retry_queue.clear()
            if not pending:
                break
            requeue: list[RetryTarget] = []
            for index, item in enumerate(pending):
                item.attempts += 1
                outcome = await self._retry_one(item)
                if outcome.ok:
                    sent += 1
                    try:
                        await self._remember_retry(item)
                    except Exception as exc:  # noqa: BLE001 - 回写失败不能打挂队列
                        log.warning(
                            "限流重试成功但回写失败（%s %s）: %s",
                            item.message_type,
                            item.target_id,
                            exc,
                        )
                elif (
                    outcome.retcode == RATE_LIMIT_RETCODE
                    and item.attempts < max_rounds
                ):
                    requeue.append(item)
                else:
                    log.warning(
                        "限流重试放弃（%s %s，第 %s 次）: %s",
                        item.message_type,
                        item.target_id,
                        item.attempts,
                        outcome.detail or outcome.retcode,
                    )
                # 只有「后面还有要发的」才需要等限流窗口；最后一条成功就不用再睡
                if interval and (requeue or index + 1 < len(pending)):
                    await asyncio.sleep(interval)
            self._retry_queue.extend(requeue)
            if not self._retry_queue:
                break
            log.info(
                "限流重试第 %s 轮结束，仍有 %s 个目标待重发",
                round_no + 1,
                len(self._retry_queue),
            )
        return sent

    async def _retry_one(self, item: RetryTarget) -> SendOutcome:
        try:
            return await self.forward_single_msg(
                event=item.event,
                message_type=item.message_type,
                group_id=item.target_id if item.message_type == "group" else None,
                user_id=item.target_id if item.message_type == "private" else None,
            )
        except Exception as exc:  # noqa: BLE001 - 重试失败不能打挂队列
            return SendOutcome(ok=False, retcode=None, detail=str(exc))

    async def _remember_retry(self, item: RetryTarget) -> None:
        """重试成功后补登记：图片按 (身份, 会话)，聊天记录按指纹 + 群。"""
        if item.kind == "image":
            if not item.identities:
                return
            chat = (
                chat_key(group_id=item.target_id)
                if item.message_type == "group"
                else chat_key(user_id=item.target_id)
            )
            if chat:
                await self._get_image_store().remember_async(item.identities, [chat])
            return
        if item.kind == "record" and item.fingerprint is not None:
            groups = [item.target_id] if item.message_type == "group" else []
            await self.forward_dedup_record(
                item.fingerprint,
                item.event,
                target_groupid=groups,
                db_idx=item.db_idx,
            )

    # ════════════════════ 去重库 ════════════════════
    def _get_dedup_store(self) -> ForwardDedupStore:
        """惰性创建指纹库：不启用去重就不会建库/开文件"""
        if self._dedup_store is None:
            self._dedup_store = ForwardDedupStore(settings=self.settings)
        return self._dedup_store

    def close(self) -> None:
        """关闭过滤门和两个库（关机 / 测试收尾用）。"""
        gate_close = getattr(self.image_gate, "close", None)
        if callable(gate_close):
            try:
                gate_close()
            except Exception:
                pass
        if self._dedup_store is not None:
            self._dedup_store.close()
        if self._image_store is not None:
            self._image_store.close()

    @staticmethod
    def _dedup_marked_groups(target_groupid=None, event=None) -> list[str]:
        """这次要标记进库的群 = 目标群 + 事件所在群（纯数字、去重保序）。

        库里存的就是这一条：「**这条聊天记录被标记的群聊**」。搬进去的目标群算标记过，
        这份内容从哪个群搬出来的也算标记过 —— 两者都得有，判定才成立：
        - 只标目标群 → “同一份内容在原来那个群又发了一遍”认不出来（发不了【发过了喵】）；
        - 只标来源群 → “这份内容已经搬进过那几个群”又无从得知。
        """
        return clean_group_ids(
            list(clean_group_ids(target_groupid))
            + list(clean_group_ids([(event or {}).get("group_id")]))
        )

    @staticmethod
    def _dedup_cur_group(event) -> str:
        """“该群” = 本次事件所在的群，只用来判要不要发【发过了喵】。"""
        groups = clean_group_ids([(event or {}).get("group_id")])
        return groups[0] if groups else ""

    def _forward_kind(self, event, miniapp_info: dict | None = None) -> str | None:
        """判定转发类型，决定指纹怎么拼：miniapp / old_forward / multimsg"""
        info = miniapp_info if miniapp_info is not None else self._check_is_miniapp(event)
        if info.get("title") == "哔哩哔哩":
            return "miniapp"
        if self._check_is_old_forward(event):
            return "old_forward"
        # 用 detail 而不是 resid 判断：实测有 resid 为空的合并转发卡片
        if get_multimsg_detail(event):
            return "multimsg"
        return None

    @staticmethod
    def _forward_cq_id(event) -> str:
        """旧版转发卡片里 [CQ:forward,id=xxx] 的 id"""
        for seg in event.get("message", []) or []:
            if seg.get("type") == "forward":
                return str((seg.get("data") or {}).get("id") or "")
        return ""

    async def _fetch_forward_messages(
        self, event, kind: str = "old_forward"
    ) -> tuple[list, str]:
        """回查 get_forward_msg，取回转发记录里的真实内容（一次 API 调用）。

        以前这里回查的是 get_msg，指望补出卡片 raw（摘要在
        raw.elements[*].multiForwardMsgElement.xmlContent 里）。线上实测 raw 不可靠：
        实时推送的事件带 raw，断点回放的历史消息不带，而且带 raw 的那条也可能
        压根没有 multiForwardMsgElement（生产日志里 raw_head="{}" / raw_keys=[]）。
        改成直接查内容：返回的每条就是一整个消息事件（有 sender / raw_message /
        message），拼出来的 `浅奈: [图片]` 与卡片里的 <title> 是同一种东西，
        而且 raw 有没有都不影响。

        注意：这里**只取一层**，给「整卡重建 / 搬运」复用（需要保留嵌套结构）。
        去重闸门要的是叶子内容，请用 :meth:`_expand_forward_for_dedup`，否则
        SnowLuma 的嵌套记录只能拿到内层 ``forward id``，看不到图片 URL。

        :return: (转发内容列表, 诊断信息)——诊断信息只写日志，拿不到内容时用来定位
        """
        candidates: list = []
        if kind == "multimsg":
            resid = get_multimsg_resid(event)
            if resid:
                candidates.append(resid)
        if event.get("message_id"):
            candidates.append(event["message_id"])
        cq_id = self._forward_cq_id(event)
        if cq_id:
            candidates.append(cq_id)
        # NapCat 对同一份转发内容是复用的（同一 resid 可能换成新消息 id），
        # 卡片消息 id 可能已失效 → 挨个试，第一次成功就用
        seen: set = set()
        marks: list = []
        for message_id in candidates:
            key = str(message_id)
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                await self.mp.ensure_connected()
                res = await self.api.get_forward_msg({"message_id": message_id})
            except Exception as exc:
                log.warning("取转发内容异常（id=%s）: %s", key, exc)
                marks.append(f"{key}:exc")
                continue
            status = str((res or {}).get("status") or "none")
            messages = ((res or {}).get("data") or {}).get("messages") or []
            marks.append(f"{key}:{status}/{len(messages)}")
            if status != "failed" and messages:
                return list(messages), f"get_forward_msg[{','.join(marks)}]"
        # 所有候选 id 都没拿到 → 旧版转发有时把内容直接嵌在事件里
        # （`message[*].data.content[*]`，传统转发链路的 get_forward_try 就走这条路）。
        # 不补这一手的话，weak 指纹会一张图都拿不到，直接退化成纯文本判定。
        embedded = self._embedded_forward_records(event)
        if embedded:
            return embedded, f"embedded[content]{len(embedded)}"
        return [], f"get_forward_msg[{','.join(marks) or 'no_id'}] 空"

    async def _screen_record_images(
        self, event, kind: str, messages: list
    ) -> list[FilterHit]:
        """过滤门扫聊天记录里的图——**必须用摊平到叶子的消息**。

        为什么不能直接把 :meth:`_fetch_forward_messages` 的结果传进去
        （实测于 2026-11-11，群 1128873261 的 9 张记录）：SnowLuma 的
        ``get_forward_msg`` 只给一层，嵌套记录在这一层全是 ``type=forward``
        段，真实图片藏在内层 id 后面。单层结果里 9 张记录合计只有 **2** 张图，
        摊平后有 **512** 张 —— 也就是说过滤门看到的图不到 0.4%，其余 510 张
        一张没下载、一张没判定，原样搬进了目标群。这就是「色图过滤器没过滤掉」
        的根因，不是模型阈值问题。

        摊平走 :meth:`_expand_forward_for_dedup`（``ForwardExpander``，按后端
        能力处理 SnowLuma 的「只给内层 id」和 NapCat 的「内层 id 不可再查」）。
        上限放到 :data:`FILTER_SCAN_MAX_ITEMS`：去重口径 200 条够用，过滤要的是
        「一张都不能漏」，大记录（实测单张记录最多 170 条）必须整份摊平。
        展开失败才退回单层结果：宁可少判，也不能完全不判。
        """
        scan_messages = messages
        fwd = ""
        try:
            expanded, fwd = await self._expand_forward_for_dedup(
                event, kind, max_items=FILTER_SCAN_MAX_ITEMS
            )
        except Exception as exc:  # noqa: BLE001 - 展开是增强，失败退回单层
            log.warning("过滤门展开聊天记录失败（%s），退回单层扫描", exc)
        else:
            if expanded:
                scan_messages = expanded
            else:
                log.warning("过滤门展开聊天记录为空（%s），退回单层扫描", fwd)
        hits = await self.image_gate.screen_messages(scan_messages, event=event)
        if fwd:
            log.info(
                "聊天记录 %s 过滤扫描：%s 条消息（%s），命中 %s 张图",
                event.get("message_id"),
                len(scan_messages or []),
                fwd,
                len(hits),
            )
        return hits

    async def _expand_forward_for_dedup(
        self, event, kind: str = "old_forward", *, max_items: int = 200
    ) -> tuple[list, str]:
        """把转发内容摊平成叶子消息，专供指纹与图片双哈希使用。

        为什么不直接复用 :meth:`_fetch_forward_messages`：

        * 后者只取一层，给「整卡重建 / 搬运」复用 —— 那里要保留嵌套结构，
          交给 ``ForwardRelay`` 按后端能力重建；
        * 去重要算的是**内容本身**。SnowLuma 的 ``get_forward_msg`` 只给内层
          ``forward id``，不内联 ``content``；只取一层时拿到的全是
          ``type=forward`` 段，图片 URL 一张都看不到（线上 ``no_image_url``
          的根因）；
        * NapCat 反过来：嵌套层直接内联 ``content``，而且拿内层 id 再发包会
          报「消息已过期或者为内层消息」。

        ``AbstractionLayer.ForwardExpander`` 已经按后端能力处理了这两种行为
        （见 ``AbstractionLayer/forward.py`` 与 ``kernal.py`` 的
        ``forward_inline_nested`` / ``forward_inner_id_fetchable``），这里直接
        复用它；展开失败才退回原来的单层取法。
        """
        kernel = getattr(self.api, "kernel", None)
        if kernel is not None:
            try:
                from AbstractionLayer import ForwardExpander

                expander = ForwardExpander(kernel, max_items=max(200, int(max_items)))
                if kind == "multimsg":
                    # multimsg 卡片本身没有 type=forward 段，用 detail.resid 当
                    # 根 id 展开；否则嵌套的图片/视频永远只能看到一层。
                    resid = get_multimsg_resid(event)
                    expansion = (
                        await expander.expand(resid)
                        if resid
                        else await expander.expand_event(event)
                    )
                else:
                    expansion = await expander.expand_event(event)
            except Exception as exc:  # noqa: BLE001 - 展开失败不能挡住搬运
                log.warning("抽象层展开异常（%s），退回单层取内容", exc)
            else:
                if expansion.messages:
                    return list(expansion.messages), f"expand[{expansion.describe()}]"
                if expansion.errors:
                    log.warning(
                        "抽象层没展开出内容（%s），退回单层取内容",
                        "；".join(expansion.errors),
                    )
        messages, fetched = await self._fetch_forward_messages(event, kind)
        return messages, f"{fetched}|fallback"

    @staticmethod
    def _embedded_forward_records(event) -> list[dict]:
        """从事件本身挖出内层转发记录（get_forward_msg 拿不到时的兜底）。

        旧版转发的每条记录也可能直接嵌在事件里：`message[*].data.content[*]`，
        每项一般还是一整个消息事件（有 sender / message），与传统转发链路的
        get_forward_try 一样。取不到任何一条就返回空列表。
        """
        records: list[dict] = []
        for seg in (event or {}).get("message") or []:
            if not isinstance(seg, dict):
                continue
            data = seg.get("data")
            content = data.get("content") if isinstance(data, dict) else None
            if isinstance(content, list):
                records.extend(item for item in content if isinstance(item, dict))
        return records

    @classmethod
    def _collect_image_urls(cls, node, urls: list[str], limit: int) -> None:
        """递归收集转发记录里的图片地址（NapCat 转发内容里是 type=image 的段）。

        取 `data.url`；有的版本只给 `data.file`（里面就是那条 http 地址），所以
        file 看着像链接时也收。非 http 的（file:// / 本地缓存路径）照样收进来，
        由调用方过滤并写进日志 —— "5 张图全下不动" 时才看得出是 url 本身不行。
        """
        if limit <= 0 or len(urls) >= limit:
            return
        if isinstance(node, dict):
            if node.get("type") == "image":
                data = node.get("data") or {}
                for key in ("url", "file"):
                    url = str(data.get(key) or "").strip()
                    if url and url not in urls:
                        urls.append(url)
                        break
            for value in node.values():
                cls._collect_image_urls(value, urls, limit)
        elif isinstance(node, (list, tuple)):
            for item in node:
                cls._collect_image_urls(item, urls, limit)

    @classmethod
    def _collect_video_urls(cls, node, urls: list[str], limit: int) -> None:
        """递归收集转发记录里的视频地址（type=video 的段）。"""
        if limit <= 0 or len(urls) >= limit:
            return
        if isinstance(node, dict):
            if node.get("type") == "video":
                data = node.get("data") or {}
                for key in ("url", "file"):
                    url = str(data.get(key) or "").strip()
                    if url and url not in urls:
                        urls.append(url)
                        break
            for value in node.values():
                cls._collect_video_urls(value, urls, limit)
        elif isinstance(node, (list, tuple)):
            for item in node:
                cls._collect_video_urls(item, urls, limit)

    @classmethod
    def _count_media_segments(cls, messages) -> tuple[int, int]:
        """数一数转发内容里真实的图片/视频段数（不是占位符个数，用于判断该不该算指纹）。"""
        image_cnt = 0
        video_cnt = 0

        def walk(node) -> None:
            nonlocal image_cnt, video_cnt
            if isinstance(node, dict):
                if node.get("type") == "image":
                    image_cnt += 1
                elif node.get("type") == "video":
                    video_cnt += 1
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(messages or [])
        return image_cnt, video_cnt

    async def _clean_for_dedup(self, messages: list, event=None) -> tuple[list, dict]:
        """入库清洗：把广告 / 占位卡片从转发内容里去掉，只用于**判定**。

        为什么放在这里而不是拼指纹的函数里：二维码检测要下载图片（阻塞 IO），
        得扔到线程里；指纹构建本身是纯函数，不该突然开始联网。

        规则（详见 :func:`messaging.forward_filter.clean_forward_messages`）：

        * 装扮 / 气泡分享卡片 —— 任意位置都丢（线上一条记录塞 32 张，正文一个没有）；
        * 末尾 ``scan_tail`` 条里的二维码海报、群推荐卡片、纯链接 / 吆喝 + 链接。

        清洗失败（依赖缺失、下载异常）时原样返回，宁可少清也不能把整条记录清空。
        """
        store = self._get_dedup_store()
        opts = store.clean_options
        report: dict = {"total": len(messages or []), "kept": len(messages or []), "removed": []}
        if not opts.get("enable", True) or not messages:
            return messages, report
        kwargs = {k: v for k, v in opts.items() if k != "enable"}
        try:
            cleaned, removed = await asyncio.to_thread(
                clean_forward_messages, messages, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - 清洗是增强，失败不能挡住搬运
            log.warning("入库清洗失败，按原始内容判定: %s", exc)
            return messages, report
        if not cleaned:
            log.warning("清洗后一条不剩，按原始内容判定")
            return messages, report
        report["kept"] = len(cleaned)
        report["removed"] = removed
        if removed:
            desc = ", ".join(f"#{i['index']}:{i['reason']}" for i in removed[:6])
            log.info("入库清洗丢掉 %s 条：%s", len(removed), desc)
            dedup_log("clean", event, kept=len(cleaned), removed=removed[:12])
        return cleaned, report

    async def _fetch_forward_image_hashes(
        self, event, kind: str, messages: list | None = None
    ) -> tuple[list[str], dict]:
        """图片双哈希校验：把转发记录里的图片算成 aHash+dHash 指纹。

        **只在文本指纹 weak（整段摘要只有 `昵称: [图片]`）时才调用**：
        这种转发的文本一模一样，分不出"同一条记录被再搬一次"和
        "另一个人发了同样张数的图"，只能靠图片本身来判。
        正常文字转发一次也不下载图片，零开销；取不到就安静返回空列表。

        :param messages: 已经取回的转发内容（gate 刚查过就直接复用，不用再打一次 API）
        :return: (图片双哈希列表, 诊断报告)。报告原样写进排查日志，用来回答
            「这次有没有找到图片 url / 每张图下载成功还是失败、为什么」。
            一张都没算出来时列表为空，判定会退回纯文本（weak 指纹此时只剩
            "谁 + 几张图"，可能误判，所以原因必须留在日志里）。
            取图路径：get_forward_msg（resid / 消息 id / CQ id 逐个试）→ 都不行
            就用事件里内嵌的 content（传统转发链路的 get_forward_try）。
        """
        available, hasher, missing_reason = _image_hash_api()
        if not available or hasher is None:
            return [], {"error": missing_reason, "url_cnt": 0, "urls": []}
        if not event:
            return [], {"error": "no_event", "url_cnt": 0, "urls": []}
        store = self._get_dedup_store()
        limit = store.img_max_count
        report: dict = {"img_max_count": limit}
        if messages is None:
            # gate 只在本地提不出摘要时才查过一次，这里是兜底（多打一次 API）。
            # 同样走抽象层：multimsg 也可能带嵌套记录，单层取不到里面的图片。
            messages, fetched = await self._expand_forward_for_dedup(event, kind)
            report["fwd"] = fetched
        report["messages"] = len(messages or [])
        # 收集上限放宽：非 http 的候选（file:// / 本地缓存路径）不该占掉下载名额
        raw_urls: list[str] = []
        self._collect_image_urls(messages or [], raw_urls, max(limit * 4, 20))
        urls = [u for u in raw_urls if u.startswith(("http://", "https://"))]
        skipped = [u for u in raw_urls if u not in urls]
        report["raw_url_cnt"] = len(raw_urls)
        if skipped:
            # 这些不是 http，下载不了 —— 写进日志，否则"没算到图片"看不出是 url 的问题
            report["skipped_cnt"] = len(skipped)
            report["skipped_urls"] = [short_url(u) for u in skipped[:10]]
        if not urls:
            report.update({"error": "no_image_url", "url_cnt": 0, "urls": []})
            return [], report
        hashes, img_report = await hasher(
            urls,
            timeout=store.img_timeout,
            max_count=limit,
            max_bytes=store.img_max_bytes,
        )
        report.update(img_report)
        # 日志里同时留短链接（一眼看域名）和原始 url 全文（手动重试下载用）
        report["url_short"] = [short_url(u) for u in urls]
        report["items"] = [
            {**item, "url_short": short_url(item.get("url", ""))}
            for item in img_report.get("items", [])
        ]
        return hashes, report

    async def _fetch_forward_video_hashes(
        self, event, kind: str, messages: list | None = None
    ) -> tuple[list[dict], dict]:
        """视频指纹校验：封面帧 + 第 10 帧，必要时再补全量字节哈希。

        **只在文本指纹 weak 时才调用**，且只处理 type=video 的段；图片不会走到
        这里。QQ 的视频地址支持 HTTP Range，ffmpeg 直接读 URL 即可，不需要先
        下载整个文件 —— 取两帧通常只传文件的一小部分。
        """
        hasher, missing_reason = _video_hash_api()
        if hasher is None:
            return [], {"error": missing_reason, "url_cnt": 0, "urls": []}
        if not event:
            return [], {"error": "no_event", "url_cnt": 0, "urls": []}
        store = self._get_dedup_store()
        limit = store.video_max_count
        report: dict = {"video_max_count": limit}
        if messages is None:
            messages, fetched = await self._expand_forward_for_dedup(event, kind)
            report["fwd"] = fetched
        report["messages"] = len(messages or [])
        raw_urls: list[str] = []
        self._collect_video_urls(messages or [], raw_urls, max(limit * 4, 20))
        urls = [u for u in raw_urls if u.startswith(("http://", "https://"))]
        skipped = [u for u in raw_urls if u not in urls]
        report["raw_url_cnt"] = len(raw_urls)
        if skipped:
            report["skipped_cnt"] = len(skipped)
            report["skipped_urls"] = [short_url(u) for u in skipped[:10]]
        if not urls:
            report.update({"error": "no_video_url", "url_cnt": 0, "urls": []})
            return [], report
        videos, video_report = await hasher(
            urls,
            timeout=store.video_timeout,
            max_count=limit,
            byte_max_bytes=store.video_byte_max_bytes,
        )
        report.update(video_report)
        report["url_cnt"] = len(urls)
        report["url_short"] = [short_url(u) for u in urls]
        report["items"] = [
            {**item, "url_short": short_url(item.get("source", ""))}
            for item in video_report.get("items", [])
        ]
        return videos, report

    async def forward_dedup_gate(
        self,
        event,
        miniapp_info: dict | None = None,
        target_groupid=None,
        cur_group_is_target: bool = False,
        reserve: bool = True,
    ) -> GateOutcome:
        """内容指纹闸门：返回「还要发给谁」+「要不要回一句」，而不是简单的放行/拦截。

        指纹只从事件本身提取（不含 sender/QQ 号/时间/消息 id），所以同一条内容
        被不同的人 repeat 转发也能识别。库里每行存的是「**这条聊天记录（指纹）
        + 它被标记的群聊**」，标记集合 = 搬进去的目标群 ∪ 搬出来的来源群。

        判定结果（:class:`GateOutcome`）：

        * ``targets``：传入的目标群**减去库里已经发过的群**。命中不等于全停 ——
          上次只发出去一半、后来新增了目标群，都会在这一次补齐；
        * ``extra_reply``：这条内容在这个群出现过就带上一句话（默认【发过了喵】），
          否则 ``None``。**这个群本身就是目标群时也不带**：内容本来就会送到它手里，
          再回一句只会和卡片叠着刷屏（线上 19:12 的现场）。

        :param target_groupid: 本次准备搬进的目标群（调用方已剔除事件所在群）
        :param cur_group_is_target: 事件所在群是否在配置的目标群里（决定要不要提示）
        :param reserve: 是否在判定「要搬」时立刻把目标群认领进主库。正常路径为
            True（并发去重）；recover 路径传 False —— 认领交给 RecoveryBatch 的
            临时记录，发送成功后才真正入库，避免预留目标被当成已送达。
        :return: :class:`GateOutcome`；``fingerprint`` 为 ``None`` 表示内容不足无法检验
        """
        kind = self._forward_kind(event, miniapp_info)
        if not kind:
            dedup_log("gate_skip", event, reason="kind_none")
            return GateOutcome(targets=list(target_groupid or []))
        marked_groups = self._dedup_marked_groups(target_groupid, event)
        cur_group = self._dedup_cur_group(event)
        fingerprint = build_fingerprint(event, kind, miniapp_info=miniapp_info)
        first_pass = dict(fingerprint)  # 本地提取的结果，用于对照
        raw_source = "event"
        messages: list | None = None
        #: 清洗**之前**的转发内容（只有真的洗过才和 messages 不是同一个对象）
        raw_messages: list | None = None
        #: 清洗前的指纹文本（别名）。清洗上线之前入库的老行存的是这个哈希，
        #: 只查清洗后的文本永远对不上 —— 见 ForwardDedupStore.find 的 alt_text。
        alt_text = ""
        #: 内容身份（展开后的叶子消息 id / 嵌套转发 id / 媒体文件名哈希）。
        #: weak 指纹取不到媒体锚点时，靠它认出「同一份内容换了卡片 id 从别的群进来」。
        identities: list[str] = []
        fwd_info = ""
        fwd_head = ""
        if kind == "old_forward":
            # type=forward（聊天记录卡片）在事件里既没有完整正文、也没有图片 url，
            # 而 weak 判据（正文文字数 < 图片数 x text_per_image）和图片双哈希都得靠
            # 真实内容，所以一律回查 get_forward_msg；取不到就退回卡片摘要那一路。
            # 这里用抽象层摊平到叶子：SnowLuma 的嵌套记录只取一层拿不到图片 URL。
            messages, fwd_info = await self._expand_forward_for_dedup(event, kind)
            if messages:
                raw_messages = messages
                identities = content_identities(event, raw_messages)
                # 先清洗再拼指纹：末尾的二维码海报 / 群卡片 / 链接，以及任意位置的
                # 装扮气泡占位卡片，都不该参与内容比对（否则同一份内容换个马甲就 miss）。
                messages, clean_info = await self._clean_for_dedup(messages, event)
                fwd_info = f"{fwd_info} clean[{clean_info.get('kept', 0)}/{clean_info.get('total', 0)}]"
                fwd_head = json.dumps(
                    messages[:2], ensure_ascii=False, default=str
                )[:1200]
                rebuilt = build_fingerprint(
                    event,
                    kind,
                    miniapp_info=miniapp_info,
                    forward_messages=messages,
                )
                if rebuilt["text"]:
                    fingerprint = rebuilt
                    raw_source = (
                        "abstraction_expand"
                        if fwd_info.startswith("expand[")
                        else "get_forward_msg"
                    )

        if not fingerprint["text"] and identities:
            # 摘要一个字都没解析出来，但内容身份拿到了：用身份拼一行稳定的合成指纹。
            # 否则这条只能「照常转发」，同一份纯图记录换个人从别的群再发就再搬一遍。
            fingerprint = build_fingerprint(
                event, kind, miniapp_info=miniapp_info
            )
            fingerprint["text"] = identity_text(identities)
            fingerprint["weak"] = True
            fingerprint["identity_only"] = True
            raw_source = "identity"
        if not fingerprint["text"]:
            # 摘要都没解析出来，无从比较，照常转发
            dedup_log(
                "gate_skip",
                event,
                fingerprint=fingerprint,
                reason="empty_fingerprint",
                kind=kind,
                raw_source=raw_source,
                first_pass=first_pass,
                groups=marked_groups,
                cur_group=cur_group,
                fwd=fwd_info,
                fwd_head=fwd_head,
            )
            return GateOutcome(targets=list(target_groupid or []))
        if not identities:
            # 小程序卡片 / 展开发包失败时没有 messages：退到事件里能拿到的身份
            identities = content_identities(event, raw_messages or messages)
        fingerprint["raw_source"] = raw_source  # 供日志区分摘要从哪来
        fingerprint["identities"] = identities  # 登记时一并写进 forward_content
        # 别名（清洗**之前**拼出来的那一版指纹）：清洗是后加的，它上线前入库的行
        # 存的是这一版哈希。只查清洗后的文本，这些老行永远匹配不上 —— 同一份内容
        # 每重发一次就搬一次（线上库里还有一批这样的行，原文已经找不回来了）。
        # 只有「清洗真的动过内容」或「本来就会洗的多段摘要」才算，没洗过就不查。
        alt_fp: dict | None = None
        if raw_messages is not None and raw_messages is not messages:
            alt_fp = build_fingerprint(
                event,
                kind,
                miniapp_info=miniapp_info,
                forward_messages=raw_messages,
                clean=False,
            )
        elif kind != "old_forward":
            # multimsg 走 clean_fingerprint_lines，miniapp 本来就不洗（两者算出来一样）
            alt_fp = build_fingerprint(event, kind, miniapp_info=miniapp_info, clean=False)
        if alt_fp and alt_fp["text"] and alt_fp["text"] != fingerprint["text"]:
            alt_text = alt_fp["text"]
        # 正文文字数 < 媒体数 x text_per_image（weak）时必须补一道媒体指纹：
        # 这类转发（少图/纯视频模板聊天记录）文本常常一模一样，只看文本会误判成"搬过"。
        # 图片和视频分开算：视频走封面帧+第10帧，不能再当成 [图片] 去下图片。
        images: list[str] = []
        videos: list[dict] = []
        img_report: dict = {}
        video_report: dict = {}
        image_cnt, video_cnt = self._count_media_segments(messages)
        image_required = False
        video_required = False
        # 身份合成指纹没有媒体可下：再要求「图片双哈希」只会把唯一能对上的身份判掉。
        identity_only = bool(fingerprint.get("identity_only"))
        if fingerprint.get("weak") and not identity_only:
            # 纯视频 weak 不碰图片；其余情况（含 multimsg 取不到 messages 时）
            # 保留原来的图片兜底，避免丢掉老链路的图片确认。
            if image_cnt > 0 or video_cnt == 0:
                images, img_report = await self._fetch_forward_image_hashes(
                    event, kind, messages=messages
                )
                fingerprint["img_report"] = img_report
                if fwd_info:
                    img_report.setdefault("fwd", fwd_info)
                dedup_log("image", event, kind=kind, img_cnt=len(images), img=img_report)
            if video_cnt > 0:
                videos, video_report = await self._fetch_forward_video_hashes(
                    event, kind, messages=messages
                )
                # 部分视频没解出来时不能让 1/1 冒充 3/3：直接作废，本次按没指纹处理。
                expected_videos = min(video_cnt, self._get_dedup_store().video_max_count)
                if len(videos) < expected_videos:
                    video_report["incomplete"] = {
                        "expected": expected_videos,
                        "hashed": len(videos),
                    }
                    videos = []
                fingerprint["video_report"] = video_report
                if fwd_info:
                    video_report.setdefault("fwd", fwd_info)
                dedup_log(
                    "video",
                    event,
                    kind=kind,
                    vid_cnt=len(videos),
                    vid=video_report,
                )
            fingerprint["images"] = images
            fingerprint["videos"] = videos
            image_required = image_cnt > 0 or video_cnt == 0
            video_required = video_cnt > 0
            if image_required and not images:
                # 拿不到图 → weak 没法确认，本次当"没搬过"处理（日志里留原因）
                first_fail = next(
                    (i for i in img_report.get("items", []) if not i.get("ok")), {}
                )
                log.warning(
                    "weak 指纹没算到图片双哈希（%s，url=%s 张，首个失败原因=%s），"
                    "本次不按文本判定，照常搬运",
                    img_report.get("error") or "unknown",
                    img_report.get("url_cnt", 0),
                    first_fail.get("reason") or "undefined",
                )
            if video_required and not videos:
                first_fail = next(
                    (i for i in video_report.get("items", []) if not i.get("ok")), {}
                )
                log.warning(
                    "weak 指纹没算到视频指纹（%s，url=%s 个，首个失败原因=%s），"
                    "本次不按文本判定，照常搬运",
                    video_report.get("error") or "unknown",
                    video_report.get("url_cnt", 0),
                    first_fail.get("reason") or "undefined",
                )
        try:
            matched = await self._get_dedup_store().find(
                fingerprint["text"],
                fingerprint.get("weak", False),
                images=images,
                videos=videos,
                group_ids=marked_groups,
                cur_group=cur_group,
                # 关键：判定和认领在库里合成一次原子操作。否则同一份内容从 a 群、
                # b 群几乎同时进来时，两条都会查到"没搬过"，目标群里各发一遍。
                # recover 期间由 RecoveryBatch 统一认领，这里不写主库。
                reserve=reserve,
                kind=kind,
                preview=fingerprint.get("preview", ""),
                image_required=image_required,
                video_required=video_required,
                alt_text=alt_text,
                identities=identities,
            )
        except Exception as exc:
            log.warning("查询指纹库失败，按未搬运处理: %s", exc)
            dedup_log("error", event, stage="find", error=str(exc), kind=kind)
            return GateOutcome(targets=list(target_groupid or []), fingerprint=fingerprint)
        targets = list(target_groupid or [])
        if matched:
            # 搬过了 → 两件事分开算：
            #   * 目标群：库里已经发过的那些剔掉，剩下的照样补发（命中不等于全停）；
            #   * 提示：只有「这条内容在这个群出现过」才回话，而且这个群本身就是
            #     目标群时不回（内容本来就会送到它手里，回话只会和卡片叠着刷屏）。
            recorded = set(clean_group_ids(matched.get("group_ids")))
            remaining = [g for g in targets if str(g) not in recorded]
            matched["decision"] = "hit" if matched.get("seen_here") else "skip"
            extra_reply = None
            if matched.get("seen_here") and not cur_group_is_target:
                extra_reply = self._reply("forwarded_already", "[发过了喵]")
            matched["notify"] = bool(extra_reply)
            matched["remaining_groups"] = [str(g) for g in remaining]
            if extra_reply:
                why = "该群有记录→发【发过了喵】"
            elif matched.get("seen_here"):
                why = "该群有记录且本身就是目标群→静默"
            else:
                why = "该群没记录→不提示"
            if matched.get("reason") == "claim_conflict":
                why += "（同内容刚被另一条消息认领）"
            elif matched.get("fresh"):
                why += f"（{CLAIM_FRESH_SECONDS} 秒内刚写过这条指纹）"
            log.info(
                "聊天记录命中去重（%s，相似度 %s，第 %s 次，kind=%s，预览=%r，"
                "已发过的群=%s，该群=%s），%s；剩余目标=%s",
                matched["reason"],
                matched["similarity"],
                matched["hit_count"],
                matched["kind"],
                matched["preview"],
                matched["group_ids"],
                cur_group or "(无)",
                why,
                remaining or "无",
            )
            dedup_log(
                matched["decision"],  # action: hit（有提示）/ skip（无提示）
                event,
                fingerprint=fingerprint,
                first_pass=first_pass,
                kind=kind,
                raw_source=raw_source,
                img_cnt=len(images),
                vid_cnt=len(videos),
                groups=marked_groups,
                cur_group=cur_group,
                decision=matched["decision"],
                notify=matched["notify"],
                remaining=matched["remaining_groups"],
                matched_by=matched["reason"],
                similarity=matched["similarity"],
                hit_count=matched["hit_count"],
                matched_preview=matched["preview"],
                matched_groups=matched["group_ids"],
                seen_here=matched.get("seen_here"),
                concurrent=matched.get("reason") == "claim_conflict",
                fresh=bool(matched.get("fresh")),
                fwd=fwd_info,
            )
            return GateOutcome(
                verdict="hint" if extra_reply else "miss",
                targets=remaining,
                extra_reply=extra_reply,
                fingerprint=fingerprint,
                matched=matched,
            )
        # 没搬过 → 照常搬运，搬完由 forward_dedup_record 把群标记进库
        dedup_log(
            "miss",
            event,
            fingerprint=fingerprint,
            first_pass=first_pass,
            kind=kind,
            raw_source=raw_source,
            img_cnt=len(images),
            vid_cnt=len(videos),
            # weak（正文文字数 < 媒体数 x text_per_image）必须靠媒体确认，这里把
            # "本次到底算没算图/视频"写进日志，便于事后对真实用例：cnt=0 且
            # required=true 就是"拿不到指纹，果断照常搬运"那一种。
            img_required=bool(image_required),
            vid_required=bool(video_required),
            groups=marked_groups,
            cur_group=cur_group,
            decision="forward",
            fwd=fwd_info,
            fwd_head=fwd_head,
        )
        return GateOutcome(targets=targets, fingerprint=fingerprint)

    async def forward_dedup_record(
        self,
        fingerprint: dict,
        event,
        target_groupid=None,
        source_groups=None,
        db_idx: int | None = None,
    ) -> None:
        """登记本次搬运：库里只存 8 字节 SimHash + 8 字节内容哈希，不存原文。

        同一份内容（content_hash 一致）只有一行，重复登记时把群并进原记录，
        所以「这条聊天记录被标记的群聊」是一直累积的。

        :param target_groupid: 这次真正送达的目标群
        :param source_groups: 内容来源群（默认取 event.group_id）。recover 合并后
            一份内容可能有多个来源群，必须全部带上，否则下次从另一个来源群回放时
            会认不出来。
        :param db_idx: 库内主键（聊天记录指纹行 id）。限流重试成功后直接用这个 id
            回写，不用重新按指纹查库。

        注：正常路径真正管用的是闸门里的原子认领（forward_dedup_gate 调
        find(reserve=True)）；recover 路径改为发送后才入库（见 ``_send_staged``）。
        这里再登记一次是幂等的兜底，保证"真发出去了"这个事实最终一定落到库里。
        """
        images = fingerprint.get("images") or []
        videos = fingerprint.get("videos") or []
        if source_groups is None:
            source_groups = [event.get("group_id")]
        marked_groups = clean_group_ids(
            list(clean_group_ids(target_groupid))
            + list(clean_group_ids(source_groups))
        )
        cur_group = self._dedup_cur_group(event)
        try:
            fp_id = await self._get_dedup_store().record(
                text=fingerprint["text"],
                kind=fingerprint.get("kind", ""),
                group_ids=marked_groups,
                preview=fingerprint.get("preview", ""),
                weak=fingerprint.get("weak", False),
                images=images,
                videos=videos,
                identities=fingerprint.get("identities") or [],
                db_idx=db_idx,
            )
            dedup_log(
                "record",
                event,
                fingerprint=fingerprint,
                fp_id=fp_id,
                raw_source=fingerprint.get("raw_source", "event"),
                img_cnt=len(images),
                vid_cnt=len(videos),
                groups=marked_groups,
                cur_group=cur_group,
            )
        except Exception as exc:
            log.warning("写入指纹失败: %s", exc)
            dedup_log("error", stage="record", error=str(exc))

    # ════════════════════ 图片批量搬运 ════════════════════
    async def image_forward_processor(self, event, target_id_list, msg_type="group"):
        if not self.mp.is_contain_image(event):
            return
        group_id = event["group_id"] if event.get("message_type") == "group" else None
        if msg_type == "group":
            for target_id in target_id_list:
                if target_id == group_id:
                    continue
                await self.mp.ensure_connected()
                # 一个目标失败不能影响后面的目标：逐个兜住并记账
                try:
                    await self.api.forward_group_single_msg(
                        group_id=target_id, message_id=event["message_id"]
                    )
                except Exception as exc:  # noqa: BLE001 - 继续搬下一个群
                    log.warning("图片搬运到群 %s 失败: %s", target_id, exc)
            return
        if msg_type == "private":
            for target_id in target_id_list:
                await self.mp.ensure_connected()
                try:
                    await self.api.forward_friend_single_msg(
                        user_id=target_id, message_id=event["message_id"]
                    )
                except Exception as exc:  # noqa: BLE001 - 继续搬下一个好友
                    log.warning("图片搬运到好友 %s 失败: %s", target_id, exc)

    async def image_forward_batch_processor(self, event, ret_info: list):
        if not self.mp.is_contain_image(event) or self.mp.is_contain_video(event):
            return
        ret_info.append(
            {
                "type": "node",
                "data": {
                    "uin": 10004,
                    "name": "N552AA🍥🍜🍣🍕💊🍫",
                    "content": event.get("message", "消息丢失喵"),
                },
            }
        )

    async def image_forward_batch(
        self, group_id, target_id_list, begin_id, end_id, msg_type
    ):
        message_content = []
        await self.mp.get_long_history_only_processor(
            group_id=group_id,
            start_id=begin_id,
            end_id=end_id,
            processor=self.image_forward_batch_processor,
            params={"ret_info": message_content},
        )
        log.info(
            "群 %s 区间 %s → %s 收集到 %s 张图",
            group_id,
            begin_id,
            end_id,
            len(message_content),
        )
        if not message_content:
            # 区间里一张图都没有时 NapCat 会拒收空聊天记录，这里直接说清楚
            log.warning("区间内没有可搬运的图片，不发送")
            return

        if msg_type == "group":
            targets = [int(t) for t in (target_id_list or [])]
            if not targets:
                log.warning("没有目标群，不发送（检查 -g / -a 配置）")
                return
            for target_id in targets:
                # 注意：img_fd 是显式命令，允许把区间内容再发回原群。
                # 旧逻辑在这里 `if target_id == group_id: continue` 会静默跳过，
                # 表现为「日志一切正常但群里什么都没有」，所以不再跳过。
                if not await self._send_batch_to_group(target_id, message_content):
                    log.warning("图片搬运到群 %s 发送失败", target_id)
            return
        if msg_type == "private":
            log.warning("私聊目标暂未实现（只支持 -g / -a）")
            return

    async def _send_batch_to_group(self, group_id, nodes: list) -> bool:
        """把合并好的图片转发到群；超时按已发出处理（NapCat 常常只是回执慢）。"""
        try:
            await self.mp.ensure_connected()
            res = await self.api.send_group_forward_msg(
                group_id=group_id, messages=nodes, timeout=REBUILT_FORWARD_TIMEOUT
            )
        except TimeoutError:
            log.warning(
                "图片搬运到群 %s 等待回执超时（>%gs），按已发出处理",
                group_id,
                REBUILT_FORWARD_TIMEOUT,
            )
            return True
        except OneBotAPIError as exc:
            log.warning("图片搬运到群 %s 被拒: %s", group_id, exc)
            return False
        except Exception as exc:
            log.warning("图片搬运到群 %s 异常: %s", group_id, exc)
            return False
        status = str((res or {}).get("status") or "")
        if status == "failed" or int((res or {}).get("retcode") or 0) != 0:
            log.warning(
                "图片搬运到群 %s 发送失败: %s %s",
                group_id,
                res.get("retcode"),
                res.get("message") or res.get("wording") or "",
            )
            return False
        log.info(
            "图片搬运到群 %s 已发送，message_id=%s",
            group_id,
            (res.get("data") or {}).get("message_id"),
        )
        return True

    async def image_forward(self, group_id, target_id_list, begin_id, end_id, msg_type):
        await self.mp.get_long_history_only_processor(
            group_id=group_id,
            start_id=begin_id,
            end_id=end_id,
            processor=self.image_forward_processor,
            params={"target_id_list": target_id_list, "msg_type": msg_type},
        )
