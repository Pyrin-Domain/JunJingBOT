# -*- coding: utf-8 -*-
"""转发的「任务单」和「闸门结论」——流程里传递的两个纯数据对象。

拆出来的原因：入口需要先回答三个互相独立的问题，再决定干什么——

1. 这条事件属于监听范围吗？（群 / 私聊白名单）
2. 它是什么类型？（聊天记录 / 哔哩哔哩卡片 / 机器人图片）
3. 这次到底要发给谁？（目标群 − 消息自己所在的群 − 库里已发过的群）

1、2 在 :mod:`bot.services.forward.detect` 里回答，产出 :class:`ForwardJob`；
3 由去重闸门（聊天记录）和图片送达库（图片）回答，产出 :class:`GateOutcome`。
``ForwardService`` 只负责把结论落到发送上，不再自己拼判定条件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "KIND_RECORD",
    "KIND_MINIAPP",
    "KIND_IMAGE",
    "FORWARD_KINDS",
    "ForwardJob",
    "GateOutcome",
    "StagedForward",
    "RecoveryBatch",
]

#: 老式 / 合并聊天记录：走聊天记录指纹库
KIND_RECORD = "record"
#: 哔哩哔哩小程序卡片：同样走聊天记录指纹库（内容就是那个视频）
KIND_MINIAPP = "miniapp"
#: 白名单机器人发的图片：走独立的图片送达库，没有「发过了喵」这种回应
KIND_IMAGE = "image"

FORWARD_KINDS = (KIND_RECORD, KIND_MINIAPP, KIND_IMAGE)


@dataclass
class ForwardJob:
    """一条事件的搬运任务单。

    目标列表在构造时就**已经**剔除了「消息自己所在的群 / 人」：本群搬本群没有意义，
    以前是靠每个发送函数各自 ``if group_id == event.group_id: continue`` 兜的，
    现在统一在识别阶段做掉，后面的函数不用再关心。
    """

    event: dict
    kind: str = KIND_RECORD
    is_recover: bool = False
    #: 这次要搬进去的群（已剔除来源群，后续还可能被去重闸门再削一轮）
    target_groups: list = field(default_factory=list)
    #: 这次要搬进去的私聊（已剔除消息发送者本人）
    target_users: list = field(default_factory=list)
    #: 是否启用内容去重（聊天记录门有效；图片门始终按送达库判）
    dedup: bool = False
    #: 事件所在群本身是不是配置的目标群。目标群里的消息命中后不回【发过了喵】：
    #: 那份内容本来就会送到这个群，提示只会和卡片叠着刷屏。
    self_group_is_target: bool = False
    #: 非空表示「识别出来了但明确不处理」，值是原因（如 sender_filtered）
    blocked: str | None = None
    miniapp_info: dict = field(default_factory=dict)
    strip_tail_ads: dict | None = None
    bot_config: dict = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.kind == KIND_IMAGE

    @property
    def has_targets(self) -> bool:
        return bool(self.target_groups or self.target_users)


@dataclass
class GateOutcome:
    """闸门结论：这次还要发给谁 + 要不要在群里回一句。

    ``verdict`` 只有两个值（对外契约，对应设计里的 hint / miss）：

    * ``"hint"``：这条内容在这个群已经出现过，``extra_reply`` 带上要回的话
      （默认「[发过了喵]」）；
    * ``"miss"``：这个群没有记录，``extra_reply`` 为 ``None``。

    注意 ``targets`` 是**减完的结果**：库里已发过的群会被剔掉，所以「命中」不等于
    「什么都不发」——只在剩下的群补齐，这正是「上次只发出去一半」能自愈的原因。
    """

    verdict: str = "miss"
    targets: list = field(default_factory=list)
    extra_reply: str | None = None
    fingerprint: dict | None = None
    matched: dict = field(default_factory=dict)

    @property
    def forwardable(self) -> bool:
        return bool(self.targets)


@dataclass
class StagedForward:
    """断点恢复里「已入库、等着发」的一条内容。

    恢复是**多群并发**的：同一份聊天记录可能同时出现在 A 群和 B 群的待回放列表里。
    如果每条事件进来就「入库 → 立刻发」，「A 群先认领」和「B 群也判没搬过」会交错，
    结果三个目标群各收到两遍（线上 17:14 的三张卡就是这样）。

    所以恢复改两阶段：**先全部入库（认领），入库完再统一算目标群**。这个对象就是
    两阶段之间传的东西——目标群在第二阶段才确定，不再等于「每个事件各算一次」。
    """

    job: ForwardJob
    #: 入库闸门返回的结论（第二阶段算目标用）
    outcome: GateOutcome = field(default_factory=GateOutcome)
    #: 这条内容在本批里的内容身份（去重用）
    identities: tuple = ()
    #: 各事件报上来的目标群（已剔除来源群）；第二阶段取它们的并集
    target_groups: tuple = ()
    #: 各事件报上来的私聊目标（已剔除发送者）
    target_users: tuple = ()
    #: 第一阶段（入库）时，这份内容已经标记过的群（含本批来源群）。
    #: 第二阶段拿它做减法：已经送过的目标群不再发。
    recorded_groups: tuple = ()
    #: 发送前要用的裁剪 / 追加配置
    strip_tail_ads: dict | None = None

    @property
    def event(self) -> dict:
        return self.job.event


@dataclass
class RecoveryBatch:
    """一次断点恢复的总账：先收齐所有群的待发内容，再统一发送。

    只负责记账，不做 IO：收料（``add``）→ 定稿（``finalize``）→ 逐条取（``drain``）。
    """

    #: key = 内容身份（没有身份时退回指纹文本），value = StagedForward
    staged: dict = field(default_factory=dict)
    #: 已经入库（认领）完成、可以进入发送阶段
    sealed: bool = False
    #: 还在跑的回收任务数：归零才允许 flush（断点恢复的各个群是并发跑的）
    pending: int = 0

    def add(self, item: StagedForward) -> StagedForward:
        """收一条待发内容；同一份内容只留一份（目标群合并）。"""
        key = "|".join(item.identities) or (
            (item.outcome.fingerprint or {}).get("text") or ""
        )
        existing = self.staged.get(key)
        if existing is None:
            item.target_groups = tuple(dict.fromkeys(item.target_groups))
            item.target_users = tuple(dict.fromkeys(item.target_users))
            self.staged[key] = item
            return item
        merged_groups = tuple(
            dict.fromkeys(list(existing.target_groups) + list(item.target_groups))
        )
        merged_users = tuple(
            dict.fromkeys(list(existing.target_users) + list(item.target_users))
        )
        merged_recorded = tuple(
            dict.fromkeys(list(existing.recorded_groups) + list(item.recorded_groups))
        )
        existing.target_groups = merged_groups
        existing.target_users = merged_users
        existing.recorded_groups = merged_recorded
        return existing

    def finalize(self) -> None:
        self.sealed = True

    def drain(self) -> list:
        """取走全部待发内容（按入库顺序），清空批次。"""
        items = list(self.staged.values())
        self.staged.clear()
        return items

    def __len__(self) -> int:
        return len(self.staged)
