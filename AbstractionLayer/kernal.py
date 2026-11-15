"""协议差异内核：后端能力档案 + 统一发包 + 返回归一化。

这一层的存在理由（实测于 2026-10-26，NapCat:3000 / SnowLuma:3210）：

``get_group_msg_history`` 在两个后端上返回的字段含义完全不同：

=============  ====================================  ================================
字段            NapCat                                SnowLuma
=============  ====================================  ================================
``message_id``  int32 哈希（可能为负，本后端唯一）     int32 哈希（**与 NapCat 不同**）
``message_seq`` = 哈希，**不是**全局序号              = 全局序号 184/185/…（int）
``real_seq``    全局序号 "184"（str），与上者一致     **不存在**
``is_reserve``  传了也没用                            传了也没用
=============  ====================================  ================================

也就是说：

- 「全局序号」在 NapCat 里叫 ``real_seq``，在 SnowLuma 里叫 ``message_seq``；
- NapCat 的 ``message_seq`` 名不副实，它其实是哈希；
- **唯一能翻页的字段两个后端不一样**，发错字段不会报错，而是被静默忽略、
  返回「以锚点为中心的一页」（NapCat 收到 ``message_id`` 时就是这样），
  这正是断点恢复行为不一致的根因。

所以本模块只做三件事：

1. 用 :class:`BackendCapabilities` 把差异写成数据；
2. 用 :meth:`OneBotKernel.detect` 在运行时**实测**出来（不靠猜硬件版本）；
3. 用 :meth:`OneBotKernel.call` 把 ``{"status","retcode","data","message","wording"}``
   统一成 :class:`ApiResult`，让上层只判断 ``ok``。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

__all__ = [
    "ApiResult",
    "BACKEND_PRESETS",
    "BackendCapabilities",
    "CallableTransport",
    "DEFAULT_ENDPOINTS",
    "GENERIC",
    "HttpTransport",
    "NAPCAT",
    "OneBotKernel",
    "SNOWLUMA",
    "MUTATING_ACTIONS",
    "Transport",
    "capabilities_for",
    "default_token",
]


#: 有副作用、且失败必须让调用方知道的动作。
#:
#: 这些动作的共同点：**失败不会自己重来，也不会有人补发**。发出去就是发出去了，
#: 失败就是没发。反过来 ``get_*`` 的失败大多是正常业务（消息撤回了、查不到人），
#: 那些不该抛异常。
#:
#: 实测教训：SnowLuma 群文件上传失败返回 ``{"status":"ok","retcode":-134}``，
#: 旧代码不校验，于是「发送成功」是假的，既没重试也没回退成下载链接。
MUTATING_ACTIONS: frozenset[str] = frozenset(
    {
        "send_group_msg",
        "send_private_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "forward_group_single_msg",
        "forward_friend_single_msg",
        # 闪传：create 会真正上传文件，send 会发一张卡片消息，失败都不能当成功
        "create_flash_task",
        "send_flash_msg",
        # 戳一戳在 OneBot 里按会话分成两个 action，不是 send_poke
        "group_poke",
        "friend_poke",
        "delete_msg",
        "set_group_ban",
        "set_msg_emoji_like",
        # 收藏表情：SnowLuma 的专用动作，发出去就是一条消息，失败不能当成功
        "send_custom_face",
    }
)


# ══════════════════════════════════════════════════════════
# 能力档案
# ══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BackendCapabilities:
    """一个 OneBot 后端在「历史 / 转发」上的全部差异。

    字段全部是**实测结论**，不是协议文档推测。新增后端时先跑
    ``python -m AbstractionLayer.cli probe --backend xxx`` 看实测值。
    """

    id: str
    label: str

    # ── 历史翻页 ──
    #: 唯一有效的游标字段名。发错字段不会报错，而是静默返回居中窗口。
    history_cursor: str = "message_seq"
    #: 全局序号（跨后端可比）寄存在哪个字段里；None 表示拿不到。
    history_global_seq_field: str | None = "real_seq"
    #: 单页条数建议上限。超了 NapCat 会超时（count=500 直接空返回）。
    history_max_count: int = 100
    #: 返回结果里是否包含作为锚点的那条消息（实测两个后端都包含）。
    history_anchor_inclusive: bool = True
    #: 锚点出现在哪一端：True=末位（reverse_order=True 时），用于自检。
    history_anchor_at_end: bool = True
    #: 是否支持 is_reserve（实测两个后端都不支持，传了被忽略）。
    history_accepts_is_reserve: bool = False
    #: 返回条数可能少于请求（后端过滤了撤回/系统消息），不代表没有更早的消息。
    history_may_return_fewer: bool = True
    #: 不带游标取「最新一条」时，返回的**是不是服务端**最新消息。
    #:
    #: - True：可以「先取最新、再往回翻到断点」，而且游标是现取的，不受
    #:   重启后 id 变化影响（NapCat 用这个）。
    #: - False：返回值可能只是后端**本地观测过**的最新消息，断点之后的新消息
    #:   它根本没见过（SnowLuma 关闭登录历史同步时）。这时必须从断点**往后**翻。
    history_latest_is_authoritative: bool = True

    # ── 转发展开 ──
    #: get_forward_msg 是否把下一层 content 直接内联返回。
    forward_inline_nested: bool = False
    #: 能否用「内层 forward id」再次调用 get_forward_msg。
    forward_inner_id_fetchable: bool = True
    #: forward id 的形态，仅用于日志提示。
    forward_id_style: str = "numeric"

    # ── 搬运（把一张聊天记录卡片转投到别的群 / 人）──
    #: 整卡搬运（``forward_group_single_msg``）时，卡片里**嵌套的聊天记录**
    #: 是否还保持可展开。False 表示会被降级成 XML 文本，接收方显示「[XML消息]」。
    relay_single_preserves_nested: bool = True
    #: ``send_group_forward_msg`` 的 node.content 能否再放一个 node（嵌套两层）。
    relay_accepts_nested_node: bool = True
    #: ``send_group_forward_msg`` 的 node.content 能否放 ``{"type":"forward"}`` 段。
    relay_accepts_forward_segment: bool = True

    # ── 媒体发送（本地视频 / 文件）──
    #: 发送通道的尝试顺序。每个值对应一种消息段：
    #:
    #: - ``"file"``：群文件（走文件上传通道，有独立配额）；
    #: - ``"video"``：视频消息（NTV2 群视频通道，与群文件配额互不影响）。
    #:
    #: 实测：群文件通道失败时，改发视频消息（NTV2 独立通道）通常还能成功；
    #: NapCat 同一文件通常直接成功。顺序放这里，业务层不再写死。
    media_send_order: tuple[str, ...] = ("file", "video")
    #: 某个通道失败后重试几次（不包括第一次）。瞬时风控/限频靠它兜。
    media_retry_times: int = 1
    #: 每次重试前的等待秒数。
    media_retry_delay: float = 1.0
    #: 视频消息段的体积上限；超过就不再尝试该通道（QQ 侧直接拒收）。
    #: ``0`` 表示不限制。
    video_max_bytes: int = 100 * 1024 * 1024

    # ── 普通文件发送（PDF / ZIP 等，没有视频段可退）──
    #: 发送通道的尝试顺序。每个值对应一条链路：
    #:
    #: - ``"file"``：群文件 / 私聊文件（与 :meth:`media_send_order` 的 ``file`` 同一条）；
    #: - ``"flash"``：闪传（``create_flash_task`` + ``send_flash_msg``，独立上传服务，
    #:   与群文件配额互不影响，失败时只剩这一条路）。
    #:
    #: 文件通道失败时（明确拒绝 / 配额 / 网络），闪传通常还能用；未知后端
    #: 保守地只走 ``file``。``-134`` 的直接原因（file 段缺 name）已由
    #: :func:`AbstractionLayer.segments.normalize_outgoing` 统一补上。
    file_send_order: tuple[str, ...] = ("file", "flash")
    #: ``create_flash_task`` 返回里 fileset id 的点分路径。
    #:
    #: SnowLuma 直接回 ``{"fileset_id": ...}``；NapCat 透传内核结构
    #: （``createFlashTransferResult.fileSetId``），所以要按后端取。
    flash_fileset_path: str = "fileset_id"

    # ── 收藏表情（账号自定义表情）──
    #: 发送收藏表情的**专用动作名**；None 表示后端没有专用动作，走 image 段兜底。
    #:
    #: 实测（2026-11-01）：SnowLuma 有 ``send_custom_face(emoji_id, group_id|user_id)``；
    #: NapCat 没有（调用返回 ``retcode=200「不支持的Api send_custom_face」``）。
    custom_face_send_action: str | None = None
    #: 专用动作里承载「表情标识」的参数名。
    custom_face_send_id_param: str = "emoji_id"
    #: ``fetch_custom_face_detail`` 里承载「账号内表情标识」的字段名。
    #:
    #: SnowLuma 给的是 ``emoji_id``（等于 resId）；NapCat **没有** ``emoji_id``，
    #: 只有 ``resId``（外加账号内序号 ``emoId`` 和本地路径 ``emoPath``）。
    custom_face_id_field: str = "emoji_id"
    #: 兜底 image 段的 ``sub_type``。**1 = 收藏表情**，0 会退化成普通图片。
    #:
    #: 实测：同一个 QQ 表情 URL，带 ``sub_type=1`` 发出去回读是
    #: ``sub_type=1 / summary="[动画表情]"``（收藏表情）；不带就是
    #: ``sub_type=0 / summary="[图片]"``（普通图片）。
    custom_face_sub_type: int = 1
    #: 兜底 image 段的 ``summary``：收藏表情的显示摘要。
    custom_face_summary: str = "[动画表情]"
    #: 抓收藏表情时的 ``count`` 上限（两个后端默认都是 48）。
    custom_face_max_count: int = 200

    # ── 失败可见性 ──
    #: 这些动作的 ``retcode != 0`` 必须当**失败**抛错，不能当成功返回。
    #:
    #: 为什么需要这张表：OneBot 的响应是 ``{"status":"ok","retcode":-134}`` 这种
    #: 形状也会出现（SnowLuma 的群文件失败就是），不校验就等于「静默丢消息」。
    #: 但 ``get_*`` 类接口的失败是**正常业务**（消息被撤回了、查不到陌生人），
    #: 一刀切会让上层到处 try/except，所以按动作列表来。
    strict_actions: frozenset[str] = field(default_factory=lambda: MUTATING_ACTIONS)

    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "history_cursor": self.history_cursor,
            "history_global_seq_field": self.history_global_seq_field,
            "history_max_count": self.history_max_count,
            "history_anchor_inclusive": self.history_anchor_inclusive,
            "history_anchor_at_end": self.history_anchor_at_end,
            "history_accepts_is_reserve": self.history_accepts_is_reserve,
            "history_may_return_fewer": self.history_may_return_fewer,
            "history_latest_is_authoritative": self.history_latest_is_authoritative,
            "forward_inline_nested": self.forward_inline_nested,
            "forward_inner_id_fetchable": self.forward_inner_id_fetchable,
            "forward_id_style": self.forward_id_style,
            "relay_single_preserves_nested": self.relay_single_preserves_nested,
            "relay_accepts_nested_node": self.relay_accepts_nested_node,
            "relay_accepts_forward_segment": self.relay_accepts_forward_segment,
            "media_send_order": list(self.media_send_order),
            "media_retry_times": self.media_retry_times,
            "media_retry_delay": self.media_retry_delay,
            "video_max_bytes": self.video_max_bytes,
            "file_send_order": list(self.file_send_order),
            "flash_fileset_path": self.flash_fileset_path,
            "custom_face_send_action": self.custom_face_send_action,
            "custom_face_send_id_param": self.custom_face_send_id_param,
            "custom_face_id_field": self.custom_face_id_field,
            "custom_face_sub_type": self.custom_face_sub_type,
            "custom_face_summary": self.custom_face_summary,
            "custom_face_max_count": self.custom_face_max_count,
            "strict_actions": sorted(self.strict_actions),
        }


NAPCAT = BackendCapabilities(
    id="napcat",
    label="NapCat",
    history_cursor="message_seq",  # 实测：它的 message_seq 就是哈希
    history_global_seq_field="real_seq",  # 实测：只有这里才是全局序号
    history_max_count=100,
    history_anchor_inclusive=True,
    history_anchor_at_end=True,
    history_accepts_is_reserve=False,
    history_may_return_fewer=True,
    # 无游标时查的是服务端，拿到的才是真·最新；而它的 message_id / message_seq
    # 每次重启都会重新分配，断点里存的旧 id 不能当游标。所以只能用「先取最新、
    # 再往回翻到断点」。
    history_latest_is_authoritative=True,
    forward_inline_nested=True,  # 实测：嵌套层 content 直接内联
    forward_inner_id_fetchable=False,  # 实测：内层 id 报「消息已过期或者为内层消息」
    forward_id_style="numeric",
    relay_single_preserves_nested=True,  # 实测：整卡搬运后内层仍是可展开的聊天记录
    relay_accepts_nested_node=False,  # 实测：node 套 node 会降级成「[卡片消息]」
    relay_accepts_forward_segment=True,  # 实测：node.content 放 forward 段正常
    # NapCat 的 create_flash_task 透传内核结果，fileset id 不在顶层。
    flash_fileset_path="createFlashTransferResult.fileSetId",
    # NapCat 没有 send_custom_face：实测调用返回 retcode=200
    # 「不支持的Api send_custom_face」。走 image 段兜底即可保住收藏表情外观。
    custom_face_send_action=None,
    # NapCat 的 fetch_custom_face_detail 里没有 emoji_id，只有 resId。
    custom_face_id_field="resId",
    notes=(
        "message_seq == message_id == 哈希；全局序号在 real_seq（字符串）。",
        "用 message_id 当历史游标不报错，但返回以它为中心的窗口且忽略 reverse_order。",
        "get_forward_msg(内层 id) 返回 retcode=200「消息已过期或者为内层消息」。",
        "count 超过约 300 后响应极慢，500 会直接超时空返回。",
        "整卡搬运（forward_group_single_msg）会把嵌套聊天记录保留成 json multimsg。",
        "send_group_forward_msg 的 node 里再嵌 node 会退化成「[卡片消息]」，别用。",
        "没有 send_custom_face；收藏表情只能靠 image 段（file=表情URL + sub_type=1）发。",
        "fetch_custom_face_detail 没有 emoji_id 字段，只有 resId / emoId / emoPath。",
    ),
)

SNOWLUMA = BackendCapabilities(
    id="snowluma",
    label="SnowLuma",
    history_cursor="message_id",  # 实测：真正的游标
    history_global_seq_field="message_seq",  # 实测：它的 message_seq 才是全局序号
    history_max_count=200,
    history_anchor_inclusive=True,
    history_anchor_at_end=True,
    history_accepts_is_reserve=False,
    history_may_return_fewer=False,
    # 关闭登录历史同步后，无游标只返回它**本地观测过**的最新消息；断点之后的
    # 消息它可能完全没见过，必须从断点往后翻。message_id 是
    # SHA1(sequence:group_id:event) 的 int32，重启后依然指向同一条消息。
    history_latest_is_authoritative=False,
    forward_inline_nested=False,  # 实测：只给 id，不给 content
    forward_inner_id_fetchable=True,  # 实测：能继续用内层 id 展开
    forward_id_style="opaque",  # 长 base64 串，含 + / =
    # 2.2.0-node 实测：整卡搬运会按目标会话重新 upload（resid 重生成），嵌套仍可展开。
    # 对应上游 issue #437 / commit 201d10c「rebuild merged forwards for the target chat」。
    # 1.14.x 及更早是 False（原样重发源 resid → 空壳卡），升级后此坑消失。
    relay_single_preserves_nested=True,
    relay_accepts_nested_node=True,  # 实测：node 套 node 会生成真正的双层聊天记录
    relay_accepts_forward_segment=True,  # 实测：node.content 放 forward 段也保留
    # SnowLuma 有专用动作 send_custom_face，内部就是发一个 image 段
    # {file: 表情URL, sub_type: 1, summary: "[动画表情]"}。
    custom_face_send_action="send_custom_face",
    custom_face_send_id_param="emoji_id",
    custom_face_id_field="emoji_id",
    notes=(
        "message_seq 是全局序号（int），与 NapCat 的 real_seq 完全一致。",
        "message_id 是自己的哈希，与 NapCat 的 message_id 不同，不能跨后端比较。",
        "get_forward_msg 只返回一层，嵌套层要拿 id 再调一次。",
        "入群时间之前的历史没有，断点早于入群时间时无法回放到锚点。",
        "整卡搬运含嵌套聊天记录时会把内层转成 xml(viewMultiMsg)，跨群后打不开；"
        "必须改走 send_group_forward_msg，并把内层包成嵌套 node。",
        "send_custom_face 的 emoji_id 可以传完整 ID，也可以传 32 位 MD5。",
    ),
)

#: 探测失败时的保守默认：宁可多尝试，也不要静默漏数据。
GENERIC = BackendCapabilities(
    id="generic",
    label="未知后端",
    history_cursor="message_seq",
    history_global_seq_field=None,
    history_max_count=50,
    history_anchor_inclusive=True,
    history_anchor_at_end=True,
    history_accepts_is_reserve=False,
    history_may_return_fewer=True,
    forward_inline_nested=True,
    forward_inner_id_fetchable=True,
    forward_id_style="unknown",
    # 未知后端不保证有闪传扩展，只走最稳的文件通道。
    file_send_order=("file",),
    # 未知后端不假设有专用动作：image 段兜底在两个已知后端上都验证过能用。
    custom_face_send_action=None,
    notes=(
        "未探测到后端特征，按最保守参数工作。",
        "收藏表情走 image 段兜底（file=表情URL + sub_type=1）。",
    ),
)

BACKEND_PRESETS: dict[str, BackendCapabilities] = {
    NAPCAT.id: NAPCAT,
    SNOWLUMA.id: SNOWLUMA,
    GENERIC.id: GENERIC,
}

_ALIASES = {
    "napcatqq": NAPCAT.id,
    "napcat-qq": NAPCAT.id,
    "nc": NAPCAT.id,
    "snowluna": SNOWLUMA.id,
    "snow-luma": SNOWLUMA.id,
    "sl": SNOWLUMA.id,
}

#: 实测端口：3000 是 NapCat 的 HTTP，3210 是 SnowLuma 的 HTTP。
DEFAULT_ENDPOINTS = {
    NAPCAT.id: "http://127.0.0.1:3000",
    SNOWLUMA.id: "http://127.0.0.1:3210",
}


def default_token() -> str:
    """调试用 token；生产环境请显式传入。"""
    return "tempport123456789"


def capabilities_for(name: str | None) -> BackendCapabilities:
    key = str(name or "").strip().lower()
    key = _ALIASES.get(key, key)
    return BACKEND_PRESETS.get(key, GENERIC)


# ══════════════════════════════════════════════════════════
# 统一返回
# ══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ApiResult:
    """``call_api`` 的归一化结果。

    NapCat 失败时给 ``status="failed"`` + ``message``；SnowLuma 有时只给
    ``wording`` 甚至只给 ``retcode``。上层不关心这些差异，只看 ``ok``。
    """

    ok: bool
    action: str
    data: Any = None
    status: str = ""
    retcode: int | None = None
    error: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def messages(self) -> list[dict]:
        """``get_*_msg_history`` / ``get_forward_msg`` 的消息列表。"""
        if isinstance(self.data, dict):
            value = self.data.get("messages")
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
        return []

    def raise_for_error(self) -> "ApiResult":
        if not self.ok:
            raise RuntimeError(f"{self.action} 失败：{self.error or self.status}")
        return self


def _normalize(action: str, raw: Any) -> ApiResult:
    if not isinstance(raw, dict):
        return ApiResult(ok=False, action=action, error=f"返回不是 dict：{type(raw).__name__!r}")

    if "_error" in raw:
        return ApiResult(ok=False, action=action, error=str(raw["_error"]), raw=raw)
    if "_http_error" in raw:
        body = str(raw.get("body") or "")[:200]
        return ApiResult(
            ok=False,
            action=action,
            retcode=int(raw["_http_error"]),
            error=f"HTTP {raw['_http_error']}: {body}",
            raw=raw,
        )

    status = str(raw.get("status") or "")
    retcode = raw.get("retcode")
    ok = status == "ok" and retcode in (0, None)
    error = "" if ok else str(raw.get("message") or raw.get("wording") or status or "unknown error")
    return ApiResult(
        ok=ok,
        action=action,
        data=raw.get("data"),
        status=status,
        retcode=retcode if isinstance(retcode, int) else None,
        error=error,
        raw=raw,
    )


# ══════════════════════════════════════════════════════════
# 传输
# ══════════════════════════════════════════════════════════
class Transport(Protocol):
    """只要能发一个 action 就够；``transport/connection.py`` 可直接适配。"""

    async def call(self, action: str, params: dict) -> dict: ...


class CallableTransport:
    """把一个已有的协程函数包成 Transport。

    接项目里现成的连接::

        CallableTransport(bot.api._call_api)
        CallableTransport(connection.call_api)
    """

    def __init__(self, fn: Callable[[str, dict], Awaitable[dict]], *, label: str = "callable") -> None:
        self._fn = fn
        self.label = label

    async def call(self, action: str, params: dict) -> dict:
        return await self._fn(action, params)


class HttpTransport:
    """OneBot v11 的 HTTP 版（``POST /<action>``），只用标准库。

    发包细节也在这里收口：Bearer token、超时、非 JSON 响应的兜底。
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 20.0,
        label: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.label = label or self.base_url

    async def call(self, action: str, params: dict) -> dict:
        return await asyncio.to_thread(self._call_sync, action, params)

    def _call_sync(self, action: str, params: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/{action}",
            data=json.dumps(params or {}).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return {"_http_error": exc.code, "body": exc.read().decode("utf-8", "replace")}
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            return {"_error": f"{type(exc).__name__}: {exc}"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_error": f"响应不是 JSON：{text[:200]!r}"}


# ══════════════════════════════════════════════════════════
# 内核
# ══════════════════════════════════════════════════════════
class OneBotKernel:
    """一个后端的连接 + 能力档案。

    上层（history / forward）只持有它，不再出现 ``if backend == "napcat"``。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        capabilities: BackendCapabilities | None = None,
        label: str = "",
    ) -> None:
        self.transport = transport
        self._caps = capabilities or GENERIC
        self.label = label or self._caps.label
        self.self_id: str = ""

    @classmethod
    def http(
        cls,
        base_url: str,
        token: str = "",
        *,
        backend: str | None = None,
        timeout: float = 20.0,
    ) -> "OneBotKernel":
        """按 URL 直连；``backend`` 只影响初始能力档案，之后可以 :meth:`detect`。"""
        return cls(
            HttpTransport(base_url, token, timeout=timeout, label=base_url),
            capabilities=capabilities_for(backend),
            label=base_url,
        )

    # ── 能力 ──
    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    def set_capabilities(self, caps: BackendCapabilities) -> None:
        self._caps = caps

    # ── 发包 ──
    async def call(self, action: str, params: dict | None = None) -> ApiResult:
        raw = await self.transport.call(action, params or {})
        return _normalize(action, raw)

    # ── 探测 ──
    async def detect(
        self,
        *,
        group_id: Any = None,
        verify_cursor: bool = True,
    ) -> BackendCapabilities:
        """实测后端能力。

        判据（对两个后端都成立，实测于 2026-10-26）：

        - 有 ``real_seq`` 且 ``message_seq != message_id`` → NapCat 型
        - 无 ``real_seq`` 且 ``message_seq != message_id`` → SnowLuma 型
        - ``message_seq == message_id`` 且无 ``real_seq`` → 退化型

        ``verify_cursor=True`` 时还会真发一次翻页请求，检查锚点是否落在末位；
        落在中间说明发错了字段（后端静默忽略），于是换另一个字段重试。
        """
        login = await self.call("get_login_info")
        if login.ok and isinstance(login.data, dict):
            self.self_id = str(login.data.get("user_id") or "")

        if group_id is None:
            groups = await self.call("get_group_list")
            if groups.ok and isinstance(groups.data, list):
                for item in groups.data:
                    if isinstance(item, dict) and item.get("group_id"):
                        group_id = item["group_id"]
                        break

        sample: list[dict] = []
        if group_id is not None:
            fetched = await self.call(
                "get_group_msg_history",
                {"group_id": group_id, "count": 10, "reverse_order": False},
            )
            sample = fetched.messages

        caps = _classify(sample)
        if verify_cursor and len(sample) >= 3 and group_id is not None:
            caps = await self._verify_cursor(caps, group_id, sample)

        self._caps = caps
        return caps

    async def _verify_cursor(
        self,
        caps: BackendCapabilities,
        group_id: Any,
        sample: list[dict],
    ) -> BackendCapabilities:
        from .cursor import MessageCursor

        anchor = sample[len(sample) // 2]
        anchor_cursor = MessageCursor.from_message(anchor)
        candidates = [caps.history_cursor]
        for name in ("message_seq", "message_id"):
            if name not in candidates:
                candidates.append(name)

        for field_name in candidates:
            value = anchor_cursor.native_value(field_name)
            if value in (None, "", 0):
                continue
            result = await self.call(
                "get_group_msg_history",
                {
                    "group_id": group_id,
                    "count": min(5, caps.history_max_count),
                    "reverse_order": True,
                    field_name: value,
                },
            )
            page = result.messages
            if not page:
                continue
            page = _sorted_ascending(page)
            if MessageCursor.from_message(page[-1]).matches(anchor_cursor):
                return replace(
                    caps,
                    history_cursor=field_name,
                    history_global_seq_field=_global_seq_field(sample),
                )
        return caps


def _global_seq_field(sample: list[dict]) -> str | None:
    if not sample:
        return None
    if all("real_seq" in item for item in sample):
        return "real_seq"
    # SnowLuma：message_seq 只要不等于 message_id，就说明它是全局序号
    if all(
        item.get("message_seq") is not None and item.get("message_seq") != item.get("message_id")
        for item in sample
    ):
        return "message_seq"
    return None


def _sorted_ascending(messages: list[dict]) -> list[dict]:
    """按全局序号升序；拿不到全局序号就按时间；再不行保持原顺序。"""
    from .cursor import MessageCursor

    cursors = [MessageCursor.from_message(m) for m in messages]
    if all(c.is_global_seq_known for c in cursors):
        order = sorted(range(len(messages)), key=lambda i: (cursors[i].real_seq, i))
    elif all(m.get("time") is not None for m in messages):
        order = sorted(range(len(messages)), key=lambda i: (messages[i]["time"], i))
    else:
        return messages
    return [messages[i] for i in order]


def _classify(sample: list[dict]) -> BackendCapabilities:
    """按样本消息的字段形状判定后端类型。

    实测的两种真实形状：

    - NapCat：``real_seq`` 存在，``message_seq == message_id == 哈希``
    - SnowLuma：``real_seq`` 不存在，``message_seq`` 是全局序号、``!= message_id``
    """
    if not sample:
        return GENERIC

    has_real_seq = all("real_seq" in item for item in sample)
    seq_equals_id = all(
        item.get("message_seq") is not None and item.get("message_seq") == item.get("message_id")
        for item in sample
    )

    if has_real_seq:
        if seq_equals_id:
            return NAPCAT
        # 有 real_seq 但 message_seq 不是哈希：另一种序号体系，游标仍需 message_seq。
        return replace(
            NAPCAT,
            id="real-seq-variant",
            label="NapCat 变体",
            notes=(*NAPCAT.notes, "message_seq 与 message_id 不同，但 real_seq 存在。"),
        )

    if not seq_equals_id:
        return SNOWLUMA

    # 既没有 real_seq，message_seq 又等于 message_id：完全拿不到全局序号。
    return replace(
        SNOWLUMA,
        id="hash-only",
        label="无全局序号后端",
        history_cursor="message_seq",
        history_global_seq_field=None,
        notes=("没有 real_seq，且 message_seq == message_id，无法得到跨后端可比的全局序号。",),
    )
