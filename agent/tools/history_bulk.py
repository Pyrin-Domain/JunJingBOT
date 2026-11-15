"""批量历史工具：``fetch_history`` / ``digest_user``。

``get_msg_history`` 一次只能拉一页（协议端实际给 50 条上下），想看上千条消息
就得让模型自己翻二十轮 —— 慢，而且很容易翻乱（游标发错字段后端不报错，只
静默返回一页居中窗口）。这里补两个工具，**都不改动 ``get_msg_history``**：

* ``fetch_history``：一次拉够 N 条，内部按后端单页上限自动翻页；
* ``digest_user``：翻一大段，只挑某个人的发言和相关对话（他回复谁 / 谁 @ 他 /
  谁回复他），并附上代码算出来的统计（发言数、平均字数、口头禅、最常互动的人），
  用来分析说话风格或做总结。

翻页本身交给 :class:`AbstractionLayer.HistoryPager`：锚点剔除、方向自检、
单页条数夹取、游标没前进就收尾都在那里，这里只负责「攒够就停」和渲染。
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from messaging.models import (
    ChatMessage,
    Quote,
    message_summary,
    parse_event,
    render_message,
)

from agent.result_vault import thread_key

from .registry import (
    _MISSING,
    api_of,
    current_context,
    get_cached_tool_result,
    messaging_of,
    record_tool_call,
    set_cached_tool_result,
    tool,
)

logger = logging.getLogger("QQBotAgent.Tools.HistoryBulk")

#: 单次最多攒多少条。1 万条约 200 页（后端单页 50 条），耗时线性增长，
#: 所以另有「页数预算」和「时长预算」两道兜底：翻不完会如实说明翻到哪。
MAX_FETCH = 10000
#: 一次翻页最多发几页请求（后端单页 20~100 条，1 万条最坏要 500 页）。
MAX_PAGES = 600
#: 翻页总时长兜底（秒）。到点就先返回已拿到的部分，别让群里干等。
FETCH_TIME_BUDGET = 240.0
#: ``fetch_history`` 回给模型的默认字符预算（中文约 1 字 = 1 token）。
FETCH_MAX_CHARS = 12000
#: ``digest_user`` 的默认字符预算（只留相关消息，可以给窄一点）。
DIGEST_MAX_CHARS = 6000
#: 单条历史消息的正文上限（和 ``<history>`` 的渲染保持一致）。
MESSAGE_CHARS = 200

_SENT_SPLIT = re.compile(r"[，。！？、,.!?；;~～\s]+")
_QQ_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")


# ══════════════════════════════════════════════════════════
# 翻页
# ══════════════════════════════════════════════════════════
@dataclass
class BulkResult:
    """一次批量翻页的结果。``messages`` 升序（旧 → 新）。"""

    messages: list[dict] = field(default_factory=list)
    pages: int = 0
    #: 已经翻到群历史起点（再往前没有了）。
    exhausted: bool = False
    #: 非空表示中途出错，已拿到的部分仍然有效。
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


async def _ensure_cursor(agent: Any, group_id: Any) -> None:
    """第一次翻页前实测一次游标字段（配置可能写错，错了翻页会原地打转）。

    ``verify_history_cursor`` 自己缓存结果，第二次调用直接返回；探测失败
    不影响业务（``HistoryPager`` 还有方向自检兜底）。
    """
    api = api_of(agent)
    verify = getattr(api, "verify_history_cursor", None)
    if not callable(verify):
        return
    if getattr(api, "_history_cursor_probed", None):
        return
    try:
        await verify(group_id)
    except Exception as exc:  # noqa: BLE001 - 探测失败不该挡住工具
        logger.warning(f"[历史] 游标探测失败，按配置值翻页: {exc}")


def _pager(agent: Any):
    """构造只为「翻历史」服务的分页器；拿不到 API 时返回 None。"""
    api = api_of(agent)
    kernel = getattr(api, "kernel", None)
    if api is None or kernel is None:
        return None

    from AbstractionLayer import HistoryPager, OneBotKernel

    caps = kernel.capabilities
    # 配置里声明的 backend 可能写错（或后端换了），实测出的游标字段优先：
    # 发错字段不会报错，只会静默返回「以锚点为中心的窗口」，翻页就在原地打转。
    anchor = str(getattr(api, "history_anchor", "") or "")
    if anchor and anchor != caps.history_cursor:
        kernel = OneBotKernel(
            kernel.transport,
            capabilities=replace(caps, history_cursor=anchor),
            label=kernel.label,
        )

    messaging = messaging_of(agent)

    async def _before_page() -> None:
        ensure = getattr(messaging, "ensure_connected", None)
        if callable(ensure):
            await ensure()
            return
        await api.ensure_connected()

    return HistoryPager(
        kernel,
        default_count=max(20, min(int(caps.history_max_count or 50), 100)),
        before_page=_before_page,
    )


def _identity(event: dict) -> tuple:
    """跨页去重用的身份（字段全等才算同一条，与抽象层一致）。"""
    from AbstractionLayer import MessageCursor

    cursor = MessageCursor.from_message(event)
    return (
        cursor.real_seq,
        str(cursor.real_id),
        str(cursor.message_id),
        str(cursor.message_seq),
        cursor.time,
    )


async def collect_messages(
    pager: Any,
    group_id: Any,
    want: int,
    *,
    before: Any = None,
) -> BulkResult:
    """从 ``before``（None = 最新）往旧翻，最多攒 ``want`` 条。"""
    limit = max(1, min(_as_int(want, 1) or 1, MAX_FETCH))
    result = BulkResult()
    cursor = before
    seen: set[tuple] = set()
    collected: list[dict] = []
    # 多留几页余量：后端可能过滤撤回/系统消息，返回比请求少得多。
    # 页数必须随目标条数一起放大——按 120 页封顶的话，单页 50 条就只能到 6000，
    # 上限调到 1 万也会被这里悄悄截断（表现为「设了 1 万只翻到 6000」）。
    max_pages = min(MAX_PAGES, max(3, limit // 10 + 10))
    deadline = time.monotonic() + FETCH_TIME_BUDGET

    for _ in range(max_pages):
        if collected and time.monotonic() > deadline:
            result.error = (
                f"翻页超时（{FETCH_TIME_BUDGET:.0f} 秒），只翻到 {len(collected)} 条"
            )
            break
        page = await pager.page(group_id, cursor=cursor, backward=True)
        result.pages += 1
        if page.error:
            result.error = page.error
            break
        if not page.messages:
            result.exhausted = True
            break

        # 页内升序（旧 → 新）；要「最近 want 条」，所以从尾部往前拿。
        for item in reversed(page.messages):
            key = _identity(item)
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
        if page.stalled:
            # 「游标没前进」不等于「到头」：后端可能刚换了 id 体系。
            # 已拿到的照常返回，但要如实告诉模型这一段可能不完整。
            result.error = "翻页游标没有前进（后端返回了同一页）"
            break
        if page.next_cursor is None:
            result.exhausted = True
            break
        cursor = page.next_cursor

    collected.reverse()
    result.messages = collected
    return result


# ══════════════════════════════════════════════════════════
# 事件 -> ChatMessage（补引用关系）
# ══════════════════════════════════════════════════════════
def _reply_id(event: dict) -> str:
    for seg in (event or {}).get("message") or []:
        if str(seg.get("type") or "") == "reply":
            value = (seg.get("data") or {}).get("id")
            if value not in (None, ""):
                return str(value)
    return ""


def _index_keys(event: dict) -> list[str]:
    keys = []
    for name in ("message_id", "real_id"):
        value = (event or {}).get(name)
        if value not in (None, ""):
            keys.append(str(value))
    return keys


def to_messages(
    events: list[dict],
    *,
    self_id: str = "",
    master_ids: tuple[str, ...] | list[str] = (),
    now: datetime | None = None,
) -> list[ChatMessage]:
    """把原始事件批量转成 ``ChatMessage``，并尽量补上「回复了谁」。"""
    now = now or datetime.now()
    messages = [
        parse_event(event, self_id=self_id, master_ids=master_ids, now=now)
        for event in events
    ]

    index: dict[str, ChatMessage] = {}
    for event, message in zip(events, messages):
        for key in _index_keys(event):
            index.setdefault(key, message)

    for event, message in zip(events, messages):
        reply_id = _reply_id(event)
        if not reply_id:
            continue
        message.reply_to_id = reply_id
        target = index.get(reply_id)
        if target is not None:
            # 补成 Quote，渲染时就是「↳谁：说了什么」，比只有一串 id 有用得多。
            message.quote = Quote(
                message_id=reply_id,
                sender=target.sender,
                summary=message_summary(target),
            )
    return messages


# ══════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════
def _namebook(messages: list[ChatMessage]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.sender.qq and message.sender.name and not message.sender.is_self:
            names.setdefault(message.sender.qq, message.sender.name)
        quote = message.quote
        if quote and quote.sender.qq and quote.sender.name:
            names.setdefault(quote.sender.qq, quote.sender.name)
    return names


def _span(messages: list[ChatMessage]) -> str:
    times = [m.time for m in messages if m.time]
    if not times:
        return "时间未知"
    start, end = min(times), max(times)
    if start.date() == end.date():
        return start.strftime("%m-%d %H:%M") + "~" + end.strftime("%H:%M")
    return start.strftime("%m-%d %H:%M") + "~" + end.strftime("%m-%d %H:%M")


def _render_lines(
    messages: list[ChatMessage],
    *,
    names: dict[str, str],
    now: datetime,
) -> list[str]:
    """逐条渲染（跨天时时间自带 ``MM-DD``，小模型才分得清是哪天）。"""
    return [
        render_message(message, names, now=now, limit=MESSAGE_CHARS)
        for message in messages
    ]


def _phrases(text: str) -> list[str]:
    """把一句话切成短句，用来统计「口头禅」（不依赖分词，够准且快）。"""
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        part = part.strip()
        if 2 <= len(part) <= 12 and not part.isdigit():
            out.append(part)
    return out


def _as_int(value: Any, default: int = 0) -> int:
    """模型给的参数可能带空格 / 中文单位，转不动就用默认值（别抛给 langchain）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════
# 跨轮留存
# ══════════════════════════════════════════════════════════
def _vault_of(agent: Any):
    return getattr(agent, "result_vault", None)


def _thread_of() -> str:
    try:
        return thread_key(current_context())
    except Exception:  # noqa: BLE001 - 拿不到会话就退化成不留存
        return ""


def _store(
    agent: Any, tool_name: str, args: dict, text: str, *, label: str = ""
) -> str:
    """把结果暂存到会话里，返回编号（拿不到 vault 时返回空串）。"""
    vault = _vault_of(agent)
    if vault is None:
        return ""
    try:
        entry = vault.put(
            _thread_of(), tool_name, text, args=args, label=label
        )
    except Exception as exc:  # noqa: BLE001 - 留存失败不能影响工具本身
        logger.warning(f"[留存] {tool_name} 暂存失败: {exc}")
        return ""
    return entry.result_id if entry is not None else ""


def _with_vault_note(text: str, result_id: str) -> str:
    """在工具结果末尾留一行编号，模型下一轮看得到、也知道怎么取回。"""
    if not result_id:
        return text
    return (
        f"{text}\n[result_id={result_id}｜这段已暂存，"
        f'下一轮要接着用就 recall_result(result_id="{result_id}")，不用重新翻]'
    )


def _parse_target(value: str) -> tuple[set[str], str]:
    """``user`` 参数 -> (QQ 集合, 昵称关键字)。"""
    text = str(value or "").strip().lstrip("@").strip()
    if not text:
        return set(), ""
    if text.isdigit() and 5 <= len(text) <= 12:
        return {text}, ""
    found = _QQ_RE.findall(text)
    if found:
        return set(found), ""
    return set(), text


def _match_targets(messages: list[ChatMessage], qq_ids: set[str], name: str) -> set[str]:
    if qq_ids:
        # 只认这一段里真出现过的：给了个没说话的号，调用方需要知道「没找到」，
        # 而不是收到一份「发言 0 条」的空摘要。
        return {m.sender.qq for m in messages if m.sender.qq in qq_ids}
    needle = name.strip().lower()
    if not needle:
        return set()
    hits = set()
    for message in messages:
        nick = (message.sender.name or "").strip().lower()
        if nick and (needle in nick or nick in needle):
            hits.add(message.sender.qq)
    return hits


# ══════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════
def build_history_bulk_tools(agent: Any) -> list:
    """批量历史工具；API 不可用时仍可构建，只是返回不可用。"""

    def _context_ids() -> tuple[str, tuple[str, ...], str]:
        context = current_context()
        self_id = str(getattr(context, "self_id", "") or "")
        masters = tuple(str(x) for x in (getattr(context, "master_ids", None) or ()))
        group_id = str(getattr(context, "group_id", "") or "")
        return self_id, masters, group_id

    @tool
    async def fetch_history(
        count: int = 200,
        group_id: int = 0,
        before_message_id: int = 0,
    ) -> str:
        """一次翻很多条群聊历史（内部自动分页，几百上千条也能翻）。
        想看较长一段时间的聊天、或者要往前翻很远时用这个；只看当前这几十条用 get_msg_history。
        参数:
          - count: int, 要往前翻多少条（默认200，最多10000）
          - group_id: int, 群号；0 表示当前群
          - before_message_id: int, 从这条消息再往前翻（0=从最新开始）
        返回按时间正序的聊天记录；开头一行说明实际翻到多少条、时间范围、有没有到头。"""
        self_id, masters, current_group = _context_ids()
        gid = _as_int(group_id) or _as_int(current_group)
        args = {"count": count, "group_id": gid, "before_message_id": before_message_id}
        logger.info(f"[工具调用] fetch_history({args})")

        if not gid:
            record_tool_call("fetch_history", args, error="当前不是群聊")
            return "[工具不可用] 当前不是群聊，fetch_history 只支持群聊历史"

        # 缓存命中就直接复用（同一轮里同样参数翻两遍没有意义）
        hit = get_cached_tool_result("fetch_history", args)
        if hit is not _MISSING:
            logger.info("[工具缓存] 复用 fetch_history 结果")
            record_tool_call("fetch_history", args, result={"cached": True})
            return hit

        # 先实测游标字段，再拿分页器：配置写错时它会被静默忽略、翻页原地打转。
        await _ensure_cursor(agent, gid)
        pager = _pager(agent)
        if pager is None:
            record_tool_call("fetch_history", args, error="历史接口不可用")
            return "[工具不可用] 历史接口不可用（API 未连接）"

        before = (
            {"message_id": before_message_id, "message_seq": before_message_id}
            if before_message_id
            else None
        )
        result = await collect_messages(pager, gid, count, before=before)
        if not result.messages:
            reason = result.error or "没有更多消息了"
            record_tool_call("fetch_history", args, error=reason)
            return f"没翻到消息：{reason}"

        messages = to_messages(
            result.messages, self_id=self_id, master_ids=masters
        )
        names = _namebook(messages)
        now = datetime.now()
        span = _span(messages)
        lines = _render_lines(messages, names=names, now=now)

        # 超预算时保留最新的部分：越新的对话越可能是模型要接的。
        picked: list[str] = []
        used = 0
        for line in reversed(lines):
            if picked and used + len(line) > FETCH_MAX_CHARS:
                break
            picked.append(line)
            used += len(line) + 1
        picked.reverse()
        omitted = len(lines) - len(picked)

        head = (
            f"翻到 {len(messages)} 条（{result.pages} 页，{span}"
            + ("，已到群历史起点" if result.exhausted else "")
            + "）"
        )
        if result.error:
            head += f"；中途停止：{result.error}"
        if omitted:
            head += (
                f"；另有更早的 {omitted} 条因篇幅未显示"
                "（完整原文已暂存，需要就 recall_result 分段取）"
            )
        if before_message_id:
            head = f"（从消息 {before_message_id} 往前）" + head

        text = head + "\n" + "\n".join(picked)
        # 暂存**完整**原文：显示被篇幅截断时，模型还能靠 recall_result 往回取，
        # 不然「已暂存」只存到最新的那一小段，翻 1 万条等于白翻。
        full_text = head + "\n" + "\n".join(lines)
        result_id = _store(
            agent,
            "fetch_history",
            args,
            full_text,
            label=f"fetch_history 群{gid} {len(messages)}条 {span}",
        )
        text = _with_vault_note(text, result_id)
        set_cached_tool_result("fetch_history", args, text)
        record_tool_call(
            "fetch_history",
            args,
            result={
                "messages": len(messages),
                "pages": result.pages,
                "exhausted": result.exhausted,
                "span": span,
            },
        )
        logger.info(
            f"[工具结果] fetch_history -> {len(messages)} 条 / {result.pages} 页（{span}）"
        )
        return text

    @tool
    async def digest_user(
        user: str = "",
        count: int = 300,
        context: int = 2,
        group_id: int = 0,
        max_chars: int = DIGEST_MAX_CHARS,
    ) -> str:
        """翻一段群聊历史，提炼某个人的发言和相关对话（用来分析说话风格 / 做总结）。
        会先自动翻 count 条消息，再挑出：他自己说的话（带前后几条上下文）、
        他回复了谁、谁 @ 了他、谁回复了他，并给出代码算好的统计（发言数、字数、口头禅、最常互动的人）。
        参数:
          - user: str, 目标用户的 QQ 号或群昵称；留空表示当前发消息的人
          - count: int, 先翻多少条群消息来找（默认300，最多10000）
          - context: int, 每条发言前后各带几条上下文（默认2，0=不带）
          - group_id: int, 群号；0 表示当前群
          - max_chars: int, 返回文本的字符预算（默认6000）
        返回：一行概览 + 统计 + 按时间正序的相关对话（★=他说的，@=提到他，缩进=上下文）。"""
        self_id, masters, current_group = _context_ids()
        gid = _as_int(group_id) or _as_int(current_group)
        wanted = _as_int(count, 300) or 300
        depth = max(0, min(_as_int(context, 2), 10))
        budget = max(1200, min(_as_int(max_chars, DIGEST_MAX_CHARS) or DIGEST_MAX_CHARS, 20000))
        args = {
            "user": str(user or ""),
            "count": wanted,
            "context": depth,
            "group_id": gid,
            "max_chars": budget,
        }
        logger.info(f"[工具调用] digest_user({args})")

        if not gid:
            record_tool_call("digest_user", args, error="当前不是群聊")
            return "[工具不可用] 当前不是群聊，digest_user 只支持群聊历史"

        hit = get_cached_tool_result("digest_user", args)
        if hit is not _MISSING:
            logger.info("[工具缓存] 复用 digest_user 结果")
            record_tool_call("digest_user", args, result={"cached": True})
            return hit

        await _ensure_cursor(agent, gid)
        pager = _pager(agent)
        if pager is None:
            record_tool_call("digest_user", args, error="历史接口不可用")
            return "[工具不可用] 历史接口不可用（API 未连接）"

        result = await collect_messages(pager, gid, wanted)
        if not result.messages:
            reason = result.error or "没有更多消息了"
            record_tool_call("digest_user", args, error=reason)
            return f"没翻到消息：{reason}"

        messages = to_messages(result.messages, self_id=self_id, master_ids=masters)
        names = _namebook(messages)
        now = datetime.now()

        # 目标：显式给了就按给的；没给就用「这条消息的发送者」。
        qq_ids, name = _parse_target(user)
        if not qq_ids and not name:
            context_obj = current_context()
            sender = getattr(getattr(context_obj, "current", None), "sender", None)
            if sender is not None and sender.qq:
                qq_ids = {str(sender.qq)}
            else:
                record_tool_call("digest_user", args, error="没指定目标用户")
                return "没指定要提炼谁：user 传 QQ 号或群昵称（留空只能用在「有人正说话」的场合）"

        targets = _match_targets(messages, qq_ids, name)
        if not targets:
            record_tool_call("digest_user", args, error="目标用户没在这一段里出现")
            return (
                f"翻了 {len(messages)} 条（{_span(messages)}），没找到 {user or '目标用户'} 的发言。"
                "换个昵称/QQ，或者把 count 调大再试。"
            )

        target_names = {
            m.sender.qq: m.sender.name for m in messages if m.sender.qq in targets and m.sender.name
        }
        who = "、".join(
            f"{target_names.get(qq) or (names.get(qq) or '未知')}({qq})" for qq in sorted(targets)
        )

        # 索引：谁说了什么，用来判断「回复的是不是他」。
        by_id: dict[str, ChatMessage] = {}
        for message in messages:
            if message.message_id:
                by_id.setdefault(str(message.message_id), message)

        core: set[int] = set()      # 他说的 + @他 / 回复他
        near: set[int] = set()      # 上下文
        his: list[int] = []
        quoted_by: Counter[str] = Counter()   # 他回复了谁
        mentioned_by: Counter[str] = Counter()  # 谁 @ 了他
        replied_by: Counter[str] = Counter()    # 谁回复了他

        for index, message in enumerate(messages):
            is_his = message.sender.qq in targets
            if is_his:
                his.append(index)
                core.add(index)
                if message.quote and message.quote.sender.qq:
                    quoted_by[message.quote.sender.label(names)] += 1
                for offset in range(1, depth + 1):
                        for pos in (index - offset, index + offset):
                            if 0 <= pos < len(messages):
                                near.add(pos)
                continue

            at_him = any(qq in targets for qq in message.mentions)
            reply_target = by_id.get(str(message.reply_to_id)) if message.reply_to_id else None
            replies_him = reply_target is not None and reply_target.sender.qq in targets
            if at_him or replies_him:
                core.add(index)
                if at_him:
                    mentioned_by[message.sender.label(names)] += 1
                if replies_him:
                    replied_by[message.sender.label(names)] += 1
                # @他 / 回复他的那条，上下文也带上（常常是完整的一问一答）
                for offset in range(1, depth + 1):
                    for pos in (index - offset, index + offset):
                        if 0 <= pos < len(messages):
                            near.add(pos)

        # 第一轮填核心行（他自己的话 / 别人对他说的），第二轮用剩下的预算补上下文。
        marks: dict[int, str] = {}
        used = 0
        shown_core = 0
        for index in sorted(core, reverse=True):
            line = render_message(
                messages[index], names, now=now, limit=MESSAGE_CHARS
            )
            prefix = "★ " if index in his else "@ "
            if used + len(line) + len(prefix) > budget * 0.8 and shown_core:
                break
            marks[index] = prefix
            used += len(line) + len(prefix) + 1
            shown_core += 1

        shown_near = 0
        for index in sorted(near - set(marks), reverse=True):
            line = render_message(
                messages[index], names, now=now, limit=MESSAGE_CHARS
            )
            if used + len(line) > budget:
                break
            marks[index] = "  "
            used += len(line) + 1
            shown_near += 1

        lines = []
        for index in sorted(marks):
            # 不再单独插「— 11-01 —」：render_message 对非今天的消息本来就会带 MM-DD。
            line = render_message(
                messages[index], names, now=now, limit=MESSAGE_CHARS
            )
            lines.append(f"{marks[index]}{line}")

        # ── 统计（全部由代码算，别让模型自己数）──
        texts = [messages[i].text for i in his]
        images = sum(1 for i in his if messages[i].images)
        stickers = sum(
            1 for i in his for seg in messages[i].segments if seg.type == "mface"
        )
        mentions = sum(1 for i in his if messages[i].mentions)
        avg = int(sum(len(t) for t in texts) / len(texts)) if texts else 0
        counter: Counter[str] = Counter()
        for text in texts:
            counter.update(_phrases(text))
        catch = "、".join(f"{w}×{n}" for w, n in counter.most_common(6) if n >= 2) or "无"

        stat = [
            f"发言 {len(his)}/{len(messages)} 条（{int(len(his) * 100 / max(1, len(messages)))}%），"
            f"平均 {avg} 字，带图 {images}，表情 {stickers}，@别人 {mentions}",
            f"口头禅：{catch}",
        ]
        if quoted_by:
            stat.append(
                "最常回复：" + "、".join(f"{k}×{v}" for k, v in quoted_by.most_common(4))
            )
        if mentioned_by:
            stat.append(
                "@他最多：" + "、".join(f"{k}×{v}" for k, v in mentioned_by.most_common(4))
            )
        if replied_by:
            stat.append(
                "回复他最多：" + "、".join(f"{k}×{v}" for k, v in replied_by.most_common(4))
            )

        hidden = len(core) - shown_core
        head = [
            f"<digest 群={gid} 目标={who} 区间={_span(messages)} 翻到={len(messages)}条"
            + ("，已到历史起点" if result.exhausted else "")
            + ">",
            *stat,
            f"相关对话 {len(core)} 条（@他/回复他 {len(core) - len(his)} 条）"
            + (f"，其中 {hidden} 条因篇幅未显示" if hidden > 0 else "")
            + f"；另带 {shown_near} 条上下文。★=他说的，@=提到他，缩进=上下文",
            "",
        ]
        if result.error:
            head.insert(-1, f"注意：翻页中途停止（{result.error}），以下只是已拿到的部分")

        text = "\n".join(head + lines)
        result_id = _store(
            agent,
            "digest_user",
            args,
            text,
            label=f"digest_user {who} 发言{len(his)}/{len(messages)}条",
        )
        text = _with_vault_note(text, result_id)
        set_cached_tool_result("digest_user", args, text)
        record_tool_call(
            "digest_user",
            args,
            result={
                "target": who,
                "messages": len(messages),
                "his": len(his),
                "shown": shown_core,
                "result_id": result_id,
            },
        )
        logger.info(
            f"[工具结果] digest_user -> {who} 发言 {len(his)}/{len(messages)} 条"
            f"（显示 {shown_core} 条，留存 {result_id or '无'}）"
        )
        return text

    @tool
    async def recall_result(
        result_id: str = "",
        offset: int = 0,
        max_chars: int = 6000,
    ) -> str:
        """取回之前工具调用留下的结果（上一轮 digest_user / fetch_history 翻出来的原文）。
        每轮对话本身是无状态的，工具结果不会自动带过来；但最近 30 分钟内的结果都暂存着，
        用这个工具能原样取回，不用重新翻一遍历史。
        参数:
          - result_id: str, 结果编号（形如 R3，工具返回文本末尾会写；留空取最近一条）
          - offset: int, 从第几个字符开始取（默认0，内容太长时用它分段）
          - max_chars: int, 本次最多取多少字（默认6000）
        返回：编号/时间/总字数的头 + 原文片段；后面还有内容会提示下一次的 offset。"""
        limit = max(200, min(_as_int(max_chars, 6000) or 6000, 12000))
        start = max(0, _as_int(offset))
        vault = _vault_of(agent)
        thread_id = _thread_of()
        args = {"result_id": str(result_id or ""), "offset": start, "max_chars": limit}
        logger.info(f"[工具调用] recall_result({args})")

        if vault is None:
            record_tool_call("recall_result", args, error="留存不可用")
            return "[工具不可用] 没有可用的结果暂存区，重新调一次原来的工具吧"

        entry = vault.get(thread_id, result_id)
        if entry is None:
            record_tool_call("recall_result", args, error="没有这条结果")
            logger.info(f"[工具结果] recall_result -> 未找到 {result_id or '最近一条'}")
            return (
                f"没找到{(' ' + result_id) if result_id else '最近一条'} 的结果："
                "可能已经过期（30 分钟内有效），或者不在这个会话里。"
                "需要的话重新调一次 digest_user / fetch_history。"
            )

        chunk = entry.text[start : start + limit]
        more = len(entry.text) - (start + len(chunk))
        head = (
            f"[{entry.result_id} {entry.label or entry.tool} · {entry.age_text()} · "
            f"共 {len(entry.text)} 字，本次取 {start}~{start + len(chunk)}]"
        )
        body = head + "\n" + chunk
        if more > 0:
            body += (
                f"\n…（还有 {more} 字，需要就 recall_result("
                f'result_id="{entry.result_id}", offset={start + len(chunk)}) 继续）'
            )
        record_tool_call(
            "recall_result",
            args,
            result={"result_id": entry.result_id, "chars": len(chunk), "more": more},
        )
        logger.info(
            f"[工具结果] recall_result -> {entry.result_id} 取 {len(chunk)} 字（剩 {more}）"
        )
        return body

    return [fetch_history, digest_user, recall_result]


__all__ = [
    "BulkResult",
    "build_history_bulk_tools",
    "collect_messages",
    "to_messages",
]
