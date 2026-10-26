"""转发服务（ForwardService）—— 自动搬运转发 + 内容去重 + 图片批量搬运。

从旧的 ``Minitor/ForwardService.py`` 整块搬过来，只依赖三样东西：

- MessageProcessor：单条消息的判定与收发工具（``self.mp``）
- NapCatAPI：底层 API 调用（``self.api``，连接由 BotApp 统一创建并共享）
- BotSettings：群号、是否去重、机器人白名单等可调参数

编排层只负责「什么时候调用」：事件进来 → ``ForwardService.auto_forward``，
返回 True 表示这条消息已按搬运逻辑处理完毕，调用方直接 return。

去重算法 / 库结构在 :mod:`bot.services.dedup`，这里只做判定与搬运的粘合。
"""

from __future__ import annotations

import asyncio
import json

from .dedup import (
    CLAIM_FRESH_SECONDS,
    ForwardDedupStore,
    build_fingerprint,
    clean_group_ids,
    dedup_log,
    get_multimsg_detail,
    get_multimsg_resid,
    short_url,
)
from messaging.forward_filter import (
    build_forward_nodes,
    is_enabled as filter_is_enabled,
    normalize_settings as normalize_filter_settings,
)

__all__ = ["ForwardService"]

# 重建的聊天记录动辄几十张图，NapCat 要逐张上传，默认 30 秒回执来不及
REBUILT_FORWARD_TIMEOUT = 180.0


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
        # 旧参数名：兼容旧调用方 / 旧测试
        nc=None,
        config=None,
    ):
        """
        :param mp: MessageProcessor 实例（文本处理层）
        :param api: NapCatAPI 实例（与 BotApp 共享同一条连接）
        :param settings: BotSettings（或行为相同的 dict）
        :param media: MediaService（暂未直接使用，保留给图片相关扩展）
        :param dedup: 现成的 ForwardDedupStore；不传则惰性创建 / 共享
        :param nc: 旧参数名，等价于 ``api``
        :param config: 旧参数名，等价于 ``settings``
        """
        self.mp = mp
        self.api = api if api is not None else nc
        self.settings = settings if settings is not None else config
        self.media = media
        self._dedup_store: ForwardDedupStore | None = dedup
        # 旧字段名，兼容按位置/属性访问的调用方
        self.nc = self.api
        self.config = self.settings

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
    async def auto_forward(
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
    ) -> bool:
        """自动搬运转发。

        :param strip_tail_ads: 末尾广告裁剪 + 尾部追加（``auto_forward.strip_tail_ads``
                      这一节配置，原样传进来即可）。开启后搬运不再整卡转发，
                      而是把内容取回来、掐掉末尾的二维码 / 群推荐卡片 / 纯链接，
                      再按配置在末尾追加自己的签名消息，重建成一份新聊天记录发出去。
                      一条都没丢、也没追加时，退回原来的整卡转发（零额外开销）。

        :param dedup: 是否启用「这条内容之前搬过没有」的检验，默认 False（不检验）。
                      启用后会先查指纹库（内容哈希精确命中和 SimHash 相似度），
                      纯图这类文本过少的转发还会额外算图片 aHash+dHash 双哈希
                      做二次校验。判定是**内容级**的，只有三种结果：
                      没搬过 → 搬运，并把目标群和事件所在群一起标记进库；
                      搬过了 + 该群已被标记 → 发【发过了喵】并结束；
                      搬过了 + 该群没被标记 → 直接结束（不搬运、也不发提示）。
                      旧版转发卡片本地提不出摘要时（raw 常缺失）会回查一次
                      get_forward_msg，用真实内容拼指纹，而不是直接放行。
        :param is_recover: True = 断点回放。这些消息大多是机器人没登录那段时间进来的，
                           回放是为了补搬。但**去重闸门照样生效**：断点每 10 秒才落一次盘，
                           崩溃 / 重启会把最后这 10 秒里已经搬过的消息再回放一遍，
                           整道闸门都跳过就会把同一条聊天记录搬第二次。回放时唯一的区别是
                           **不发【发过了喵】**——那不是群里有人重复发，别往群里刷提示。
        """
        DEBUG = False
        if groupid_list is None:
            groupid_list = []
        if userid_list is None:
            userid_list = []
        if target_groupid is None:
            target_groupid = []
        if task_userid is None:
            task_userid = []
        if auto_img_forward_from_bot is None:
            auto_img_forward_from_bot = {}
        # 配置里手写的 "20002" 和面板存的 20002 都当成同一个群号：
        # 统一比字符串，否则类型对不上会悄悄什么都搬不了
        message_type = event.get("message_type")
        if message_type == "group":
            if str(event.get("group_id")) not in clean_group_ids(groupid_list):
                DEBUG and print("GROUP_ID NOT CORRECT")
                return False
        elif message_type == "private":
            if str(event.get("user_id")) not in clean_group_ids(userid_list):
                DEBUG and print("USER_ID NOT CORRECT")
                return False
        else:
            return False

        if auto_img_forward_from_bot.get("enable", False):
            # 白名单里命中这个机器人、且它这条消息带图 → 直接搬走
            # （范围判断见 bot_entry_matches：group / private / all）
            if self._is_whitelisted_bot(event, auto_img_forward_from_bot) and self.mp.is_contain_image(event):
                DEBUG and print(f"Auto Forward Image from Bot: {event.get('user_id')}")
                await self.batch_forward_single_msg(event, target_groupid=target_groupid, task_userid=task_userid)
                return True

        miniapp_info = self._check_is_miniapp(event)
        match = (
            self._check_is_old_forward(event)
            or self._check_is_multimsg_forward(event)
            or miniapp_info.get("title") == "哔哩哔哩"
        )
        if not match:
            DEBUG and print("Match Failed!")
            return False

        # ── 搬运去重闸门：搬过就不再搬，区别只在要不要发【发过了喵】 ──
        fingerprint: dict | None = None
        if dedup:
            fingerprint, matched = await self.forward_dedup_gate(
                event, miniapp_info=miniapp_info, target_groupid=target_groupid
            )
            # 「搬过就不再搬」这个结论对回放同样成立：断点每 10 秒才落一次盘，
            # 崩溃 / 重启会把最后这 10 秒里**已经搬过**的消息再回放一遍。
            # 旧代码让 is_recover 整道闸门都跳过，回放就照样搬运 → 同一条聊天记录
            # 被搬两次（日志里表现为 hit 后面紧跟同 message_id 的 record）。
            # 回放时唯一该跳过的是提示：那不是群里有人重复发，别往群里发【发过了喵】。
            if matched.get("decision") == "hit":
                if not is_recover:
                    await self.mp.send_img(
                        event=event,
                        img_addr=self._img_file("news.gif"),
                        summary=self._reply("forwarded_already", "[发过了喵]"),
                    )
                return True
            if matched.get("decision") == "skip":
                return True

        forwarded = await self.forward_rebuilt(
            event,
            opts=normalize_filter_settings(strip_tail_ads),
            target_groupid=target_groupid,
            task_userid=task_userid,
        )
        if forwarded is None:
            # 没启用 / 没内容可裁也没追加 → 还是原来的整卡转发
            print("\n\nforward_single_msg\n\n")
            forwarded = await self.batch_forward_single_msg(
                event, target_groupid=target_groupid, task_userid=task_userid
            )
        # 真的发出去了才登记指纹，避免“匹配但无处可转”的消息污染指纹库
        if fingerprint is not None and forwarded:
            await self.forward_dedup_record(
                fingerprint, event, target_groupid=target_groupid
            )
        return True

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
        if not filter_is_enabled(opts):
            return None
        kind = self._forward_kind(event)
        if kind not in ("old_forward", "multimsg"):
            return None  # 小程序卡片没有「末尾几条」的概念，保持原样搬运

        messages, fetched = await self._fetch_forward_messages(event, kind)
        if not messages:
            print(f"[转发裁剪] 取不到转发内容（{fetched}），照常整卡转发")
            return None

        # 下载图片 + 解码二维码是同步阻塞的，扔到线程里别卡住事件循环
        nodes, report = await asyncio.to_thread(build_forward_nodes, messages, opts)
        if not report.get("rebuilt"):
            return None

        removed = report.get("removed") or []
        removed_desc = ", ".join(f"#{item['index']}:{item['reason']}" for item in removed) or "无"
        print(
            f"[转发裁剪] 共 {report['total']} 条，丢弃 {len(removed)} 条（{removed_desc}），"
            f"追加 {report.get('appended', 0)} 条，重建 {len(nodes)} 个节点"
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
            print(f"[转发裁剪] {unknown} 个目标没有回执（多半是上传耗时超过超时），按已发出处理，不退回整卡转发")
            return sent + unknown
        if attempts and not sent:
            print("[转发裁剪] 重建后的消息一条都没发出去，退回整卡转发")
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
        except (TimeoutError, asyncio.TimeoutError):
            print(f"[转发裁剪] 发送到{label}超时（{REBUILT_FORWARD_TIMEOUT:g} 秒没回执）")
            return None
        except Exception as exc:
            print(f"[转发裁剪] 发送到{label}异常: {exc}")
            return None
        status = str((res or {}).get("status") or "")
        if status == "failed":
            print(f"[转发裁剪] 发送到{label}失败: {(res or {}).get('message')}")
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

    # ══════════ MessageProcessor 的卡片判定（新 MessageProcessor 保留同名方法） ══════════
    def _check_is_miniapp(self, event) -> dict:
        checker = getattr(self.mp, "check_is_miniapp", None)
        if callable(checker):
            return checker(event) or {}
        from .dedup import get_miniapp_info

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
    ) -> bool:
        DEBUG = False
        if message_type == "group" and group_id is not None:
            await self.mp.ensure_connected()
            # 跨群搬「聊天记录套聊天记录」时，部分后端会把内层压成 XML
            # （SnowLuma 就是这样），接收方只看到「[XML消息]」打不开。
            # 这种情况改走重建，让内层内容自包含。
            if await self._relay_nested_forward(event, group_id=group_id):
                return True
            await self.api.forward_group_single_msg(
                group_id=group_id, message_id=event["message_id"]
            )
            refresher = getattr(self.mp, "check_point_active_get", None)
            if callable(refresher):
                await refresher(group_id)
            return True
        elif message_type == "private" and user_id is not None:
            await self.mp.ensure_connected()
            if await self._relay_nested_forward(event, user_id=user_id):
                return True
            await self.api.forward_friend_single_msg(
                user_id=user_id, message_id=event["message_id"]
            )
            return True
        return False

    async def _relay_nested_forward(self, event, *, group_id=None, user_id=None) -> bool:
        """跨会话搬卡片时，只在**必要**时改走「重建」，否则交回整卡搬运。

        背景（2026-10-26 实测 + 上游 issue #437）：

        - SnowLuma ≤ 1.14.15 的 ``forward_*_single_msg`` 是「原样重发缓存里那条
          消息」，把源会话的 resid 塞进目标会话，结果是空壳卡（桌面端打不开、
          手机端能进但图片加载失败）。上游提交 ``201d10c`` 改成「展开内层树 →
          对目标会话重新 upload 拿新 resid → 用新 resid 发」，**2.2.0 起已生效**，
          我们跑的正是 2.2.0，所以这类卡片整卡搬就能打开，不必介入。
        - 仍然救不回来的只有一种：内层已经是 ``xml/viewMultiMsg`` 死卡片
          （``m_fileName`` 指向源会话临时文件，入站时就已经降级）。
          此时任何后端整卡重发都只是再发一次坏卡，只能重建（有损，摘要文本）。

        返回 True 表示「已经用重建方式发出去了」，调用方不要再整卡搬。
        """
        target = group_id if group_id is not None else user_id
        if target is None:
            return False
        # 本群搬本群：缓存有效，整卡搬运最保真，不动。
        origin = event.get("group_id") if group_id is not None else event.get("user_id")
        if origin is not None and str(origin) == str(target):
            return False

        plan_fn = getattr(self.api, "relay_plan", None)
        nodes_fn = getattr(self.api, "relay_nodes", None)
        if not callable(plan_fn) or not callable(nodes_fn):
            return False

        kind = self._forward_kind(event)
        if kind not in ("old_forward", "multimsg"):
            return False  # 小程序卡片没有嵌套聊天记录的概念

        messages, fetched = await self._fetch_forward_messages(event, kind)
        if not messages:
            print(f"[转发搬运] 取不到内容（{fetched}），照常整卡转发")
            return False

        try:
            plan = await plan_fn(messages)
        except Exception as exc:  # noqa: BLE001 - 判定失败就退回整卡搬运
            print(f"[转发搬运] 方案判定失败（{exc}），照常整卡转发")
            return False
        if not plan.needs_rebuild:
            return False

        print(f"[转发搬运] {plan.reason}，改用重建发送")
        content = await nodes_fn(messages=messages)
        if not content.nodes:
            print(f"[转发搬运] 重建失败（{'；'.join(content.errors) or '没有节点'}），照常整卡转发")
            return False
        if content.depth_reached:
            print(f"[转发搬运] 已内联 {content.depth_reached} 层嵌套，共 {len(content.nodes)} 个节点")

        if group_id is not None:
            res = await self._send_rebuilt(group_id=group_id, nodes=content.nodes)
        else:
            res = await self._send_rebuilt(user_id=user_id, nodes=content.nodes)
        if res is False:
            print("[转发搬运] 重建发送失败，退回整卡转发")
            return False
        return True

    async def batch_forward_single_msg(
        self,
        event,
        target_groupid=None,
        task_userid=None,
    ) -> int:
        DEBUG = False
        target_groupid = target_groupid or []
        task_userid = task_userid or []
        count = 0
        for group_id in target_groupid:
            if group_id == event.get("group_id"):
                continue
            asyncio.create_task(
                self.forward_single_msg(
                    event=event, group_id=group_id, message_type="group"
                )
            )
            count += 1
            DEBUG and print("Succeed Forward!")
        for user_id in task_userid:
            if user_id == event.get("user_id"):
                continue
            asyncio.create_task(
                self.forward_single_msg(
                    event=event, user_id=user_id, message_type="private"
                )
            )
            count += 1
            DEBUG and print("Succeed Forward!")
        return count

    # ════════════════════ 去重库 ════════════════════
    def _get_dedup_store(self) -> ForwardDedupStore:
        """惰性创建指纹库：不启用去重就不会建库/开文件"""
        if self._dedup_store is None:
            self._dedup_store = ForwardDedupStore(settings=self.settings)
        return self._dedup_store

    def close(self) -> None:
        """关闭指纹库连接（关机 / 测试收尾用）。"""
        if self._dedup_store is not None:
            self._dedup_store.close()

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
                print(f"[转发去重] 取转发内容异常(id={key}): {exc}")
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
            # gate 只在本地提不出摘要时才查过一次，这里是兜底（多打一次 API）
            messages, fetched = await self._fetch_forward_messages(event, kind)
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
            urls, timeout=store.img_timeout, max_count=limit
        )
        report.update(img_report)
        # 日志里同时留短链接（一眼看域名）和原始 url 全文（手动重试下载用）
        report["url_short"] = [short_url(u) for u in urls]
        report["items"] = [
            {**item, "url_short": short_url(item.get("url", ""))}
            for item in img_report.get("items", [])
        ]
        return hashes, report

    async def forward_dedup_gate(
        self, event, miniapp_info: dict | None = None, target_groupid=None
    ) -> tuple[dict | None, dict]:
        """搬运去重闸门。

        指纹只从事件本身提取（不含 sender/QQ 号/时间/消息 id），所以同一条内容
        被不同的人 repeat 转发也能识别。库里每行存的就是「**这条聊天记录（指纹）
        + 它被标记的群聊**」，标记集合 = 搬进去的目标群 ∪ 搬出来的来源群。
        判定是**内容级**的，一共三种结果：
        - 没搬过（matched 为空）→ 调用方搬运，由 forward_dedup_record 标记进库；
        - 搬过了 + 该群（事件所在群）在标记里 → matched["decision"] == "hit"
          → 调用方发【发过了喵】并结束；
        - 搬过了 + 该群不在标记里 → matched["decision"] == "skip"
          → 调用方直接结束（**不搬运、也不发提示**）。

        :param target_groupid: 本次准备搬进的目标群号列表
        :return: (指纹信息 | None, 判定结果)，指纹为 None 表示内容不足无法检验，照常转发
        """
        kind = self._forward_kind(event, miniapp_info)
        if not kind:
            dedup_log("gate_skip", event, reason="kind_none")
            return None, {}
        marked_groups = self._dedup_marked_groups(target_groupid, event)
        cur_group = self._dedup_cur_group(event)
        fingerprint = build_fingerprint(event, kind, miniapp_info=miniapp_info)
        first_pass = dict(fingerprint)  # 本地提取的结果，用于对照
        raw_source = "event"
        messages: list | None = None
        fwd_info = ""
        fwd_head = ""
        if kind == "old_forward":
            # type=forward（聊天记录卡片）在事件里既没有完整正文、也没有图片 url，
            # 而 weak 判据（正文文字数 < 图片数 x text_per_image）和图片双哈希都得靠
            # 真实内容，所以一律回查 get_forward_msg；取不到就退回卡片摘要那一路
            messages, fwd_info = await self._fetch_forward_messages(event, kind)
            if messages:
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
                    raw_source = "get_forward_msg"
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
            return None, {}
        fingerprint["raw_source"] = raw_source  # 供日志区分摘要从哪来
        # 正文文字数 < 图片数 x text_per_image（weak）时必须补一道图片双哈希：
        # 这类转发（少图模板聊天记录）文本常常一模一样，只看文本会误判成"搬过"。
        # 这里是整条链路上唯一会下载图片的地方，且只在 weak 时触发。
        images: list[str] = []
        img_report: dict = {}
        if fingerprint.get("weak"):
            images, img_report = await self._fetch_forward_image_hashes(
                event, kind, messages=messages
            )
            fingerprint["images"] = images
            # 报告跟着指纹走：hit/miss/record 那几行日志里也能看到图片这一路的结果
            fingerprint["img_report"] = img_report
            if fwd_info:
                img_report.setdefault("fwd", fwd_info)
            if not images:
                # 拿不到图 → weak 没法确认，本次当"没搬过"处理（日志里留原因）
                first_fail = next(
                    (i for i in img_report.get("items", []) if not i.get("ok")), {}
                )
                print(
                    f"[转发去重] weak 指纹（正文文字数 < 图片数 x text_per_image）"
                    f"没算到图片双哈希（{img_report.get('error') or 'unknown'}，"
                    f"url={img_report.get('url_cnt', 0)} 张，"
                    f"首个失败原因={first_fail.get('reason') or 'undefined'}），"
                    f"本次不按文本判定，照常搬运"
                )
            dedup_log("image", event, kind=kind, img_cnt=len(images), img=img_report)
        try:
            matched = await self._get_dedup_store().find(
                fingerprint["text"],
                fingerprint.get("weak", False),
                images=images,
                group_ids=marked_groups,
                cur_group=cur_group,
                # 关键：判定和认领在库里合成一次原子操作。否则同一份内容从 a 群、
                # b 群几乎同时进来时，两条都会查到"没搬过"，目标群里各发一遍。
                reserve=True,
                kind=kind,
                preview=fingerprint.get("preview", ""),
            )
        except Exception as exc:
            print(f"[转发去重] 查询指纹库失败，按未搬运处理: {exc}")
            dedup_log("error", event, stage="find", error=str(exc), kind=kind)
            return fingerprint, {}
        if matched:
            # 搬过了 → 只有两种结局：该群在标记里就发【发过了喵】，否则直接结束
            matched["decision"] = "hit" if matched.get("seen_here") else "skip"
            if matched["decision"] == "hit":
                why = "该群已被标记→发【发过了喵】"
            else:
                why = "该群没被标记→不搬运也不发提示"
            if matched.get("reason") == "claim_conflict":
                why += "（同内容刚被另一条消息认领）"
            elif matched.get("fresh"):
                why += f"（{CLAIM_FRESH_SECONDS} 秒内刚写过这条指纹）"
            print(
                f"[转发去重] 这条聊天记录搬过（{matched['reason']}，"
                f"相似度 {matched['similarity']}，第 {matched['hit_count']} 次，"
                f"kind={matched['kind']}，预览={matched['preview']!r}，"
                f"被标记的群={matched['group_ids']}，该群={cur_group or '(无)'}）"
                f"：{why}"
            )
            dedup_log(
                matched["decision"],  # action: hit（发提示）/ skip（什么都不做）
                event,
                fingerprint=fingerprint,
                first_pass=first_pass,
                kind=kind,
                raw_source=raw_source,
                img_cnt=len(images),
                groups=marked_groups,
                cur_group=cur_group,
                decision=matched["decision"],
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
            return fingerprint, matched
        # 没搬过 → 照常搬运，搬完由 forward_dedup_record 把群标记进库
        dedup_log(
            "miss",
            event,
            fingerprint=fingerprint,
            first_pass=first_pass,
            kind=kind,
            raw_source=raw_source,
            img_cnt=len(images),
            # weak（正文文字数 < 图片数 x text_per_image）必须靠图片确认，这里把
            # "本次到底算没算图"写进日志，便于事后对真实用例：img_cnt=0 且
            # img_required=true 就是"拿不到图，果断照常搬运"那一种。
            img_required=bool(fingerprint.get("weak")),
            groups=marked_groups,
            cur_group=cur_group,
            decision="forward",
            fwd=fwd_info,
            fwd_head=fwd_head,
        )
        return fingerprint, {}

    async def forward_dedup_record(
        self, fingerprint: dict, event, target_groupid=None
    ) -> None:
        """登记本次搬运：库里只存 8 字节 SimHash + 8 字节内容哈希，不存原文。

        同一份内容（content_hash 一致）只有一行，重复登记时把群并进原记录，
        所以「这条聊天记录被标记的群聊」是一直累积的。标记的群 = 目标群 + 事件所在群。

        注：真正管用的是闸门里的原子认领（forward_dedup_gate 调 find(reserve=True)），
        那一步已经把这些群写进库了；这里再登记一次是幂等的兜底，
        保证"真发出去了"这个事实最终一定落到库里。
        """
        images = fingerprint.get("images") or []
        marked_groups = self._dedup_marked_groups(target_groupid, event)
        cur_group = self._dedup_cur_group(event)
        try:
            fp_id = await self._get_dedup_store().record(
                text=fingerprint["text"],
                kind=fingerprint.get("kind", ""),
                group_ids=marked_groups,
                preview=fingerprint.get("preview", ""),
                weak=fingerprint.get("weak", False),
                images=images,
            )
            dedup_log(
                "record",
                event,
                fingerprint=fingerprint,
                fp_id=fp_id,
                raw_source=fingerprint.get("raw_source", "event"),
                img_cnt=len(images),
                groups=marked_groups,
                cur_group=cur_group,
            )
        except Exception as exc:
            print(f"[转发去重] 写入指纹失败: {exc}")
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
                await self.api.forward_group_single_msg(
                    group_id=target_id, message_id=event["message_id"]
                )
            return
        if msg_type == "private":
            for target_id in target_id_list:
                await self.mp.ensure_connected()
                await self.api.forward_friend_single_msg(
                    user_id=target_id, message_id=event["message_id"]
                )

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
        print(
            f"[图片搬运] 群 {group_id} 区间 {begin_id} → {end_id} "
            f"收集到 {len(message_content)} 张图"
        )
        if not message_content:
            # 区间里一张图都没有时 NapCat 会拒收空聊天记录，这里直接说清楚
            print("[图片搬运] 区间内没有可搬运的图片，不发送")
            return

        if msg_type == "group":
            targets = [int(t) for t in (target_id_list or [])]
            if not targets:
                print("[图片搬运] 没有目标群，不发送（检查 -g / -a 配置）")
                return
            for target_id in targets:
                # 注意：img_fd 是显式命令，允许把区间内容再发回原群。
                # 旧逻辑在这里 `if target_id == group_id: continue` 会静默跳过，
                # 表现为「日志一切正常但群里什么都没有」，所以不再跳过。
                if not await self._send_batch_to_group(target_id, message_content):
                    print(f"[图片搬运] 群 {target_id} 发送失败")
            return
        if msg_type == "private":
            print("[图片搬运] 私聊目标暂未实现（只支持 -g / -a）")
            return

    async def _send_batch_to_group(self, group_id, nodes: list) -> bool:
        """把合并好的图片转发到群；超时按已发出处理（NapCat 常常只是回执慢）。"""
        try:
            await self.mp.ensure_connected()
            res = await self.api.send_group_forward_msg(
                group_id=group_id, messages=nodes, timeout=REBUILT_FORWARD_TIMEOUT
            )
        except TimeoutError:
            print(
                f"[图片搬运] 群 {group_id} 等待回执超时（>{REBUILT_FORWARD_TIMEOUT:g}s），"
                "按已发出处理"
            )
            return True
        except Exception as exc:
            print(f"[图片搬运] 群 {group_id} 发送异常: {exc}")
            return False
        status = str((res or {}).get("status") or "")
        if status == "failed" or int((res or {}).get("retcode") or 0) != 0:
            print(
                f"[图片搬运] 群 {group_id} 发送失败: "
                f"{res.get('retcode')} {res.get('message') or res.get('wording') or ''}"
            )
            return False
        print(f"[图片搬运] 群 {group_id} 已发送，message_id={(res.get('data') or {}).get('message_id')}")
        return True

    async def image_forward(self, group_id, target_id_list, begin_id, end_id, msg_type):
        await self.mp.get_long_history_only_processor(
            group_id=group_id,
            start_id=begin_id,
            end_id=end_id,
            processor=self.image_forward_processor,
            params={"target_id_list": target_id_list, "msg_type": msg_type},
        )
