"""OneBot / NapCat / SnowLuma API 封装。

方法名、参数、返回的 dict 与旧 ``Minitor/NapCatAPI.py``（``NapCatAPIInterface``）
保持一致，业务层不需要改调用方式。实际发送由
:class:`transport.connection.OneBotConnection` 负责（``echo`` 配对 + 超时），
这一层只拼 ``action`` / ``params``。

历史接口的游标字段由后端档案决定（NapCat=``message_seq``，SnowLuma=``message_id``）。
调用方可以同时传 ``message_seq`` / ``message_id``，这里只把当前后端认识的那个发出去。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ConnectionSettings, coerce_connection_settings
from .connection import OneBotConnection


class OneBotAPI:
    """OneBot API 接口（NapCat / SnowLuma 通用）。

    构造参数可以是 :class:`OneBotConnection`（推荐：反向模式下事件与 API 共用一条连接），
    也可以是 ``ConnectionSettings`` / dict / 配置文件路径（内部自建连接，懒启动）。
    """

    def __init__(self, connection: OneBotConnection | ConnectionSettings | dict | str | Path):
        if isinstance(connection, OneBotConnection) or hasattr(connection, "call_api"):
            self.connection: OneBotConnection = connection  # type: ignore[assignment]
        else:
            settings = coerce_connection_settings(connection)
            self.connection = OneBotConnection(settings, role="API")

        # 旧字段兼容（老 WebPanel 会读 bot.nc._rws；旧 MessageProcessor 读 _ws_conn）
        self._rws = self.connection
        self.config = getattr(self.connection, "config", None)
        self._ws_conn = None
        self._reader_task = None
        self._send_lock = None
        self._pending = None
        # 运行时实测出来的游标字段（None = 还没测过）。
        self._history_cursor_probed: str | None = None
        # 懒创建的协议内核（承载实测出的后端能力）。
        self._kernel = None

    # ── 后端信息 ──
    @property
    def backend(self) -> str:
        return str(getattr(self.connection, "backend", "") or "")

    @property
    def kernel(self):
        """协议内核：统一发包 + 实测能力，供历史 / 转发 / 搬运共用。"""
        if self._kernel is None:
            from AbstractionLayer import CallableTransport, OneBotKernel
            from AbstractionLayer.kernal import capabilities_for

            self._kernel = OneBotKernel(
                CallableTransport(self._call_api, label=self.backend or "api"),
                capabilities=capabilities_for(self.backend),
                label=self.backend or "api",
            )
        return self._kernel

    @property
    def configured_history_anchor(self) -> str:
        """配置里声明的游标字段（可能写错）。"""
        return str(getattr(self.connection, "history_anchor", "") or "message_seq")

    @property
    def history_anchor(self) -> str:
        """实际发出去的游标字段：实测值优先，没测过才用配置。

        ``get_group_msg_history`` 发错游标字段**不会报错**，而是静默返回
        「以锚点为中心的窗口」并忽略 ``reverse_order``。断点恢复因此会在
        原地打转。所以在第一次用历史接口时实测一次，见
        :meth:`verify_history_cursor`。
        """
        return self._history_cursor_probed or self.configured_history_anchor

    # ── 生命周期 ──
    async def connect(self) -> None:
        """确保连接可用（反向：等后端连上来；正向：后台连过去）。"""
        ensure = getattr(self.connection, "ensure_connected", None)
        if callable(ensure):
            await ensure()
            return
        start = getattr(self.connection, "start", None)
        if callable(start):
            await start()

    async def close(self) -> None:
        """关闭连接。

        反向模式下连接由主控方（BotApp）管理，这里不动它，避免把事件连接关掉。
        """
        if getattr(self.connection, "is_reverse", False):
            return
        stop = getattr(self.connection, "stop", None)
        if callable(stop):
            await stop()

    async def ensure_connected(self, timeout: float | None = None) -> OneBotConnection:
        ensure = getattr(self.connection, "ensure_connected", None)
        if callable(ensure):
            await ensure(timeout)
        else:  # pragma: no cover - 仅测试 fake 会走到
            await self.connect()
        return self.connection

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self.connection, "is_connected", False))

    def set_event_handler(self, handler) -> "OneBotAPI":
        self.connection.set_event_handler(handler)
        return self

    # ── 通用 ──
    async def _call_api(self, action: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if timeout is None:
            return await self.connection.call_api(action, params or {})
        return await self.connection.call_api(action, params or {}, timeout=timeout)

    # ── 历史游标实测 ──
    async def verify_history_cursor(self, group_id: Any = None) -> str:
        """实测当前后端真正认的游标字段，并缓存。

        判据来自 :mod:`AbstractionLayer.kernal` 的运行时探测：真发一次
        ``reverse_order=True`` 的翻页，如果锚点落在**末位**说明字段对，落在
        中间说明被静默忽略，就换另一个字段重试。

        探测失败（网络异常 / 消息太少）时保留配置值，不阻塞业务。
        """
        if self._history_cursor_probed:
            return self._history_cursor_probed

        try:
            caps = await self.kernel.detect(group_id=group_id)
        except Exception:  # noqa: BLE001 - 探测失败不能挡住业务
            return self.configured_history_anchor

        self._history_cursor_probed = caps.history_cursor
        if caps.history_cursor != self.configured_history_anchor:
            print(
                f"[历史游标] 配置写的是 {self.configured_history_anchor}，"
                f"实测应为 {caps.history_cursor}，已按实测值发包"
                f"（{caps.label}）"
            )
        return caps.history_cursor

    async def relay_plan(self, messages: list[dict], *, force_rebuild: bool = False):
        """决定一张聊天记录卡片该怎么搬到别的群 / 人。

        返回 :class:`AbstractionLayer.RelayPlan`：``action="single"`` 走
        ``forward_group_single_msg`` 整卡搬，``action="rebuild"`` 必须重建成
        node 再 ``send_group_forward_msg``（SnowLuma 跨群搬嵌套记录属于这种）。
        """
        from AbstractionLayer import plan_relay

        if not self._history_cursor_probed:
            await self.verify_history_cursor()
        return plan_relay(self.kernel.capabilities, messages, force_rebuild=force_rebuild)

    async def relay_nodes(self, forward_id: Any = None, *, messages: list[dict] | None = None):
        """把一张聊天记录重建成自包含的 node 列表（嵌套层也内联）。

        返回 :class:`AbstractionLayer.RelayContent`，``.nodes`` 可直接交给
        ``send_group_forward_msg``。内层内容被展开成嵌套 node，不引用源群的
        临时文件，所以跨群 / 换设备都打得开。
        """
        from AbstractionLayer import ForwardRelay

        if not self._history_cursor_probed:
            await self.verify_history_cursor()
        return await ForwardRelay(self.kernel).collect(forward_id, messages=messages)

    def reset_history_probe(self) -> None:
        """换后端 / 重连后丢掉实测结果，下次重新探测。"""
        self._history_cursor_probed = None

    # ── 发消息 ──
    async def send_group_message(
        self, group_id, message: Any, *, timeout: float | None = None
    ) -> dict:
        """发送群消息。

        :param timeout: 等响应的秒数。文件字节不过这条连接（协议端自己从本地读），
                        但带大文件时**响应要等上传结束才回来**，默认 30 秒不够。
        """
        return await self._call_api(
            action="send_group_msg",
            params={"group_id": group_id, "message": message},
            timeout=timeout,
        )

    async def send_private_message(
        self, user_id, message: Any, *, timeout: float | None = None
    ) -> dict:
        """发送私聊消息（``timeout`` 同上）。"""
        return await self._call_api(
            action="send_private_msg",
            params={"user_id": user_id, "message": message},
            timeout=timeout,
        )

    async def send_group_img(self, group_id, img_addr: str, summary: str = "") -> dict:
        """发送群图片"""
        return await self._call_api(
            action="send_group_msg",
            params={
                "group_id": group_id,
                "message": [
                    {"type": "image", "data": {"file": img_addr, "summary": summary}}
                ],
            },
        )

    async def send_private_img(self, user_id, img_addr: str, summary: str = "") -> dict:
        """发送私聊图片"""
        return await self._call_api(
            action="send_private_msg",
            params={
                "user_id": user_id,
                "message": [
                    {"type": "image", "data": {"file": img_addr, "summary": summary}}
                ],
            },
        )

    async def send_group_forward_msg(self, group_id, messages: list, timeout: float | None = None) -> dict:
        """转发群聊消息（node 列表）。

        :param timeout: 秒；重建的聊天记录可能有几十张图，NapCat 要逐张上传，
                       默认 30 秒不够用，调用方可以放宽
        """
        return await self._call_api(
            action="send_group_forward_msg",
            params={"group_id": group_id, "messages": messages},
            timeout=timeout,
        )

    async def send_private_forward_msg(self, user_id, messages: list, timeout: float | None = None) -> dict:
        """转发私聊消息（合成聊天记录，用 node 列表发送）"""
        return await self._call_api(
            action="send_private_forward_msg",
            params={"user_id": user_id, "messages": messages},
            timeout=timeout,
        )

    async def forward_group_single_msg(self, group_id, message_id) -> dict:
        """转发单条消息到群"""
        return await self._call_api(
            action="forward_group_single_msg",
            params={"group_id": group_id, "message_id": message_id},
        )

    async def forward_friend_single_msg(self, user_id, message_id) -> dict:
        """转发单条消息到好友"""
        return await self._call_api(
            action="forward_friend_single_msg",
            params={"user_id": user_id, "message_id": message_id},
        )

    # ── 消息 / 历史 ──
    async def get_message(self, message_id) -> dict:
        """获取消息详情"""
        return await self._call_api(action="get_msg", params={"message_id": message_id})

    async def get_forward_msg(self, params: dict) -> dict:
        """获取转发消息"""
        return await self._call_api(action="get_forward_msg", params={**params})

    def _history_params(
        self,
        base: dict,
        *,
        message_seq: Any = None,
        message_id: Any = None,
    ) -> dict:
        """按后端档案选择历史游标字段。

        同时传两个值时只发当前后端认识的那个；只传一个时把它当作通用游标
        （兼容旧调用：旧代码把 message_id 当 message_seq 用）。
        """
        anchor = self.history_anchor
        seq = message_seq
        mid = message_id
        if anchor == "message_id":
            value = mid if mid is not None else seq
            if value:
                base["message_id"] = value
        else:
            value = seq if seq is not None else mid
            if value:
                base["message_seq"] = value
        return base

    async def get_group_msg_history(
        self,
        group_id,
        message_seq=None,
        reverse_order: bool = False,
        count: int = 20,
        *,
        message_id=None,
    ) -> dict:
        """群聊历史。reverse_order=True 向前拉取，False 向后拉取。

        游标字段由后端决定：NapCat 用 ``message_seq``，SnowLuma 用 ``message_id``。
        """
        params = self._history_params(
            {
                "group_id": group_id,
                "reverse_order": reverse_order,
                "count": count,
            },
            message_seq=message_seq,
            message_id=message_id,
        )
        return await self._call_api(action="get_group_msg_history", params=params)

    async def get_friend_msg_history(
        self,
        user_id,
        message_seq=None,
        reverse_order: bool = False,
        count: int = 20,
        *,
        message_id=None,
    ) -> dict:
        """好友历史。reverse_order=True 向前拉取，False 向后拉取。"""
        params = self._history_params(
            {
                "user_id": user_id,
                "reverse_order": reverse_order,
                "count": count,
            },
            message_seq=message_seq,
            message_id=message_id,
        )
        return await self._call_api(action="get_friend_msg_history", params=params)

    # ── 账号 / 群 / 好友 ──
    async def get_user_id(self) -> str:
        """取 Bot 自己的 QQ 号。"""
        temp = await self._call_api(action="get_login_info", params={})
        user_id = temp["data"]["user_id"]
        return str(user_id)

    async def get_login_info(self) -> dict:
        """获取登录信息（也用于心跳保活）"""
        return await self._call_api(action="get_login_info", params={})

    async def get_group_list(self) -> dict:
        return await self._call_api(action="get_group_list", params={})

    async def get_group_member_list(self, group_id) -> dict:
        return await self._call_api(
            action="get_group_member_list", params={"group_id": group_id}
        )

    async def get_group_member_info(self, group_id, user_id) -> dict:
        return await self._call_api(
            action="get_group_member_info",
            params={"group_id": group_id, "user_id": user_id},
        )

    async def get_friend_list(self) -> dict:
        return await self._call_api(action="get_friend_list", params={})

    async def get_stranger_info(self, user_id) -> dict:
        return await self._call_api(
            action="get_stranger_info", params={"user_id": user_id}
        )

    async def get_group_info(self, group_id) -> dict:
        return await self._call_api(
            action="get_group_info", params={"group_id": group_id}
        )

    # ── 管理 / 互动 ──
    async def delete_msg(self, message_id) -> dict:
        """撤回消息（群管理员可撤回其他人的，普通成员只能撤自己的）"""
        return await self._call_api(
            action="delete_msg", params={"message_id": message_id}
        )

    async def send_poke(self, user_id, group_id=None) -> dict:
        """戳一戳：传 group_id 用 group_poke（群聊），否则用 friend_poke（私聊）"""
        if group_id:
            return await self._call_api(
                action="group_poke",
                params={"group_id": group_id, "user_id": user_id},
            )
        return await self._call_api(action="friend_poke", params={"user_id": user_id})

    async def set_msg_emoji_like(
        self, message_id, emoji_id, set_: bool = True, **kwargs
    ) -> dict:
        """对指定消息做表情回应（NapCat: set_msg_emoji_like）"""
        if "set" in kwargs:
            set_ = bool(kwargs.pop("set"))
        return await self._call_api(
            action="set_msg_emoji_like",
            params={
                "message_id": message_id,
                "emoji_id": str(emoji_id),
                "set": set_,
            },
        )

    async def set_group_ban(self, group_id, user_id, duration: int = 60) -> dict:
        """禁言群成员：duration 单位秒，0 表示解除禁言"""
        return await self._call_api(
            action="set_group_ban",
            params={
                "group_id": group_id,
                "user_id": user_id,
                "duration": int(duration),
            },
        )

    async def get_mini_app_ark(self, params: dict) -> dict:
        """获取小程序 Ark 卡片"""
        return await self._call_api(action="get_mini_app_ark", params=params)


# 旧名字，方便迁移期对照；新代码建议用 OneBotAPI。
NapCatAPI = OneBotAPI
NapCatAPIInterface = OneBotAPI


__all__ = [
    "OneBotAPI",
    "NapCatAPI",
    "NapCatAPIInterface",
]
