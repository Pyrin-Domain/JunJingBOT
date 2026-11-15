"""归一化的历史翻页。

上层只看到一份契约，不用再猜「锚点在第几个」「这个后端认哪个字段」：

1. 返回的消息**永远按时间升序**（旧 → 新）；
2. 锚点消息**永远被剔除**（exclusive），所以翻页不会重复也不会漏；
3. 下一页游标永远从边界那条消息取，不按下标猜；
4. 单页条数自动夹到后端上限；
5. ``is_reserve`` 只在后端真的支持时才发（实测两个后端都不支持）。

第 2 条是关键。原实现用 ``messages.pop(0)`` 去掉「上一批已处理的锚点」，
这等于假设锚点固定在第 0 位。实测两个后端在 ``reverse_order=True`` 时锚点都在
**末位**，于是每页都会丢掉最旧的一条。

第 4 条也是实测踩出来的：NapCat ``count=500`` 会超时并返回空，看起来像
「没有历史了」，实际只是请求太大。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .cursor import MessageCursor
from .kernal import OneBotKernel, _sorted_ascending

__all__ = ["HistoryPage", "HistoryPager", "ReplayResult", "WalkResult"]


@dataclass
class HistoryPage:
    """一页历史。``messages`` 升序且不含锚点。"""

    messages: list[dict] = field(default_factory=list)
    #: 继续往前（backward）或往后（forward）翻要用的游标。
    next_cursor: MessageCursor | None = None
    #: 实际请求的条数（已被后台上限夹过）。
    requested: int = 0
    #: 后端原始返回条数（含锚点）。
    raw_count: int = 0
    #: 后端没给更多消息了。
    exhausted: bool = False
    #: 后端返回条数少于请求（过滤了撤回/系统消息），不代表历史到头。
    truncated: bool = False
    #: 非空表示这一页不可信（游标字段不对 / 后端忽略了游标 / 报错）。
    error: str = ""
    #: 游标没有前进：继续翻也是同一页。
    stalled: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class WalkResult:
    """从某个游标一路往回/往前翻的结果。"""

    #: 升序（旧 → 新），已剔除 ``stop_at`` 指定的那条。
    messages: list[dict] = field(default_factory=list)
    #: 是否找到了 ``stop_at``。没有 ``stop_at`` 时恒为 True。
    found: bool = True
    #: 是否翻到了历史起点。
    reached_start: bool = False
    pages: int = 0
    reason: str = ""


@dataclass
class ReplayResult:
    """断点回放要翻的那一段（升序，已剔除断点本身）。"""

    #: 断点**之后**（更新）的消息，旧 → 新，可直接按顺序回放。
    messages: list[dict] = field(default_factory=list)
    #: 是否找到断点。从断点往后翻时没有「找」这回事，恒为 True。
    found: bool = True
    #: 从最新往旧翻时，是否已经翻到历史起点（说明断点可能被清空了）。
    reached_start: bool = False
    pages: int = 0
    #: 非空表示提前收尾的原因（游标没前进 / 游标字段不对 / 接口报错）。
    reason: str = ""


class HistoryPager:
    """按归一化契约拉历史的门面。"""

    def __init__(
        self,
        kernel: OneBotKernel,
        *,
        default_count: int = 20,
        before_page: Any = None,
    ) -> None:
        self.kernel = kernel
        self.default_count = default_count
        #: 每页发出前的异步钩子（重连用）。翻页途中掉线时，下一发才能连上。
        self._before_page = before_page

    @property
    def capabilities(self):
        return self.kernel.capabilities

    # ══════════════════════════════════════════════════════
    # 单页
    # ══════════════════════════════════════════════════════
    async def page(
        self,
        group_id: Any,
        *,
        cursor: Any = None,
        backward: bool = True,
        count: int | None = None,
        is_reserve: bool | None = None,
    ) -> HistoryPage:
        """拉一页。

        :param cursor: 锚点。``None`` 表示从最新一条开始。
        :param backward: True 往旧翻（断点恢复用），False 往新翻。
        :param is_reserve: 只在后端支持时才会真的发出去。
        """
        caps = self.capabilities
        requested = max(1, min(int(count or self.default_count), caps.history_max_count))

        if self._before_page is not None:
            await self._before_page()

        params: dict[str, Any] = {
            "group_id": group_id,
            "count": requested,
            "reverse_order": bool(backward),
        }

        anchor = MessageCursor.from_message(cursor) if cursor is not None else None
        if anchor is not None:
            field_name = caps.history_cursor
            value = anchor.native_value(field_name)
            if value in (None, 0, ""):
                # 关键：不能改发另一个字段。NapCat 收到 message_id 不报错，
                # 而是返回「以它为中心」的窗口并忽略 reverse_order。
                return HistoryPage(
                    requested=requested,
                    error=(
                        f"游标缺少后端 {caps.label} 的翻页字段 {field_name} "
                        f"（{anchor.describe()}）；发别的字段会被静默忽略"
                    ),
                )
            params[field_name] = value

        if is_reserve is not None and caps.history_accepts_is_reserve:
            params["is_reserve"] = bool(is_reserve)

        result = await self.kernel.call("get_group_msg_history", params)
        if not result.ok:
            return HistoryPage(requested=requested, error=result.error or "历史接口失败")

        raw = result.messages
        page = HistoryPage(requested=requested, raw_count=len(raw))
        if not raw:
            page.exhausted = True
            return page

        ordered = _sorted_ascending(raw)
        page.truncated = len(ordered) < requested

        # 锚点按身份剔除，不按位置猜。
        if anchor is not None:
            kept = [item for item in ordered if not anchor.matches(item)]
            if len(kept) != len(ordered):
                ordered = kept
                if not ordered:
                    # 后端只回传了锚点本身 —— 说明那是历史的边界，正常到头。
                    page.exhausted = True
                    return page

        # 方向自检：往旧翻却拿到比锚点更新的东西，说明后端忽略了游标。
        if anchor is not None and ordered:
            if backward and _strictly_newer(ordered[-1], anchor):
                page.error = (
                    f"{caps.label} 忽略了游标 {caps.history_cursor}="
                    f"{anchor.native_value(caps.history_cursor)}，返回的是最新一页"
                )
                return page
            if not backward and _strictly_older(ordered[0], anchor):
                page.error = (
                    f"{caps.label} 忽略了游标 {caps.history_cursor}="
                    f"{anchor.native_value(caps.history_cursor)}，返回的是最旧一页"
                )
                return page

        page.messages = ordered
        page.next_cursor = MessageCursor.from_message(ordered[0] if backward else ordered[-1])

        if anchor is not None and page.next_cursor and page.next_cursor.matches(anchor):
            page.stalled = True
        return page

    # ══════════════════════════════════════════════════════
    # 逐条
    # ══════════════════════════════════════════════════════
    async def iter_messages(
        self,
        group_id: Any,
        *,
        start: Any = None,
        backward: bool = True,
        count: int | None = None,
        max_pages: int = 80,
        limit: int | None = None,
    ) -> AsyncIterator[dict]:
        """逐条产出，方向由 ``backward`` 决定（默认从新到旧）。"""
        cursor = MessageCursor.from_message(start) if start is not None else None
        emitted = 0
        pages = 0

        while pages < max_pages:
            pages += 1
            page = await self.page(group_id, cursor=cursor, backward=backward, count=count)
            if page.error:
                break
            if not page.messages:
                break

            ordered = page.messages if backward else list(reversed(page.messages))
            for item in ordered:
                yield item
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

            if page.stalled or page.next_cursor is None:
                break
            cursor = page.next_cursor

    async def walk(
        self,
        group_id: Any,
        *,
        start: Any = None,
        stop_at: Any = None,
        count: int | None = None,
        max_pages: int = 80,
        max_messages: int | None = None,
    ) -> WalkResult:
        """从最新（或 ``start``）一路往旧翻，直到命中 ``stop_at`` 或历史见底。

        这是断点恢复要的形状：返回 ``stop_at`` **之后**（更新）的所有消息，
        升序，可以直接按顺序回放。
        """
        target = MessageCursor.from_message(stop_at) if stop_at is not None else None
        cursor = MessageCursor.from_message(start) if start is not None else None

        collected: list[dict] = []
        result = WalkResult(found=target is None)

        for _ in range(max_pages):
            page = await self.page(group_id, cursor=cursor, backward=True, count=count)
            result.pages += 1
            if page.error:
                result.reason = page.error
                break
            if not page.messages:
                result.reached_start = True
                break

            # 页内是升序，从尾往前扫才是「新 → 旧」。
            for item in reversed(page.messages):
                if target is not None and target.matches(item):
                    result.found = True
                    break
                collected.append(item)
                if max_messages is not None and len(collected) >= max_messages:
                    result.reason = f"超过 max_messages={max_messages}，未找到锚点"
                    break
            if result.found or result.reason:
                break

            if page.stalled or page.next_cursor is None:
                result.reason = result.reason or "游标没有前进"
                break
            cursor = page.next_cursor

        collected.reverse()
        result.messages = collected
        return result

    async def replay(
        self,
        group_id: Any,
        *,
        checkpoint: Any = None,
        latest: Any = None,
        count: int | None = None,
        max_pages: int = 80,
        stall_limit: int = 2,
    ) -> ReplayResult:
        """断点回放：返回断点**之后**的消息（升序，不含断点本身）。

        方向由「调用方给不给 ``latest``」决定，因为这取决于**断点里的 id 还能不能
        当游标**——那是后端的特性，pager 不该猜：

        - 给了 ``latest``：从最新往旧翻，直到命中 ``checkpoint``。
          适合 NapCat：它的 ``message_id`` / ``message_seq`` 每次重启都会重新
          分配，断点里的旧 id 不能当游标，只能用现取的最新 id 起翻，靠
          ``real_id`` / 全局序号认出断点。
        - 没给：从 ``checkpoint`` 往新翻，直到后端没有更多消息。
          适合 SnowLuma：id 跨重启稳定，但不带游标取到的「最新」可能只是它
          **本地观测过**的消息（关掉登录历史同步时正好等于断点），信任它就会把
          整段更新跳过。

        「翻不动」连续 ``stall_limit`` 页就收尾，不硬翻 80 页：NapCat 收到错的
        游标字段不报错，而是返回「以锚点为中心的窗口」并忽略 ``reverse_order``，
        没有这道闸门就会原地打转。
        """
        target = MessageCursor.from_message(checkpoint) if checkpoint is not None else None
        backward = latest is not None
        if backward:
            cursor = MessageCursor.from_message(latest)
            if not cursor:
                return ReplayResult(found=False, reason="latest 游标为空")
        else:
            if not target:
                return ReplayResult(found=False, reason="断点游标为空")
            cursor = target

        collected: list[dict] = []
        seen: set[tuple] = set()
        result = ReplayResult(found=not backward)
        stalled = 0

        for _ in range(max_pages):
            page = await self.page(group_id, cursor=cursor, backward=backward, count=count)
            result.pages += 1
            if page.error:
                result.reason = page.error
                break
            if not page.messages:
                result.reached_start = backward
                break

            if backward:
                # 页内升序，倒着扫才是「新 → 旧」。
                for item in reversed(page.messages):
                    if target is not None and target.matches(item):
                        result.found = True
                        break
                    key = _identity(item)
                    if key not in seen:
                        seen.add(key)
                        collected.append(item)
                if result.found:
                    break
            else:
                for item in page.messages:
                    key = _identity(item)
                    if key not in seen:
                        seen.add(key)
                        collected.append(item)

            if page.stalled:
                stalled += 1
                if stalled >= stall_limit:
                    result.reason = f"游标连续 {stalled} 页没有前进"
                    break
            else:
                stalled = 0
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        # 往旧翻时是按「新 → 旧」攒的，倒回来才是升序。
        if backward:
            collected.reverse()
        result.messages = collected
        return result


def _identity(message: Any) -> tuple:
    """跨页去重用的身份。字段全等才算同一条（同一后端内足够）。"""
    cur = MessageCursor.from_message(message)
    return (cur.real_seq, str(cur.real_id), str(cur.message_id), str(cur.message_seq), cur.time)


def _seq(cursor: MessageCursor) -> int | None:
    return cursor.real_seq


def _strictly_newer(message: Any, anchor: MessageCursor) -> bool:
    cur = MessageCursor.from_message(message)
    if cur.real_seq is None or _seq(anchor) is None:
        return False
    return cur.real_seq > anchor.real_seq


def _strictly_older(message: Any, anchor: MessageCursor) -> bool:
    cur = MessageCursor.from_message(message)
    if cur.real_seq is None or _seq(anchor) is None:
        return False
    return cur.real_seq < anchor.real_seq
