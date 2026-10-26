"""把 NapCat 事件补全成 ``ChatMessage`` / ``ChatContext``。

这是「数据采集层」：只负责发包补齐引用、转发、历史，不做最终排版（排版在
``models.render_*``），也不懂 AI。

旧实现在 ``Minitor/NapCatTools.py``，依赖两个动态 import（``_chat_context_module``
/ ``_thread_history_module``）。新代码直接 import :mod:`.models` 与
:mod:`.thread_history`，模块级依赖清晰、没有循环。

所有函数第一个参数是 :class:`messaging.processor.MessageProcessor`（``mp``），
``MessageProcessor`` 上的同名方法只是薄封装。
"""

from __future__ import annotations

from datetime import datetime

from core.anchors import anchor_kwargs

from .attention import detect_attention, message_text
from .forward import (
    _fetch_forward_events,
    _forward_count_from_card,
    _get_message_event,
    check_is_miniapp,
    check_is_multimsg_forward,
    check_is_old_forward,
)
from .history import collect_history
from .models import (
    ChatContext,
    Quote,
    is_legacy_context_note,
    message_summary,
    parse_event,
)
from .thread_history import merge_history


async def collect_message(
    mp,
    event,
    *,
    cache: dict | None = None,
    miniapp: str = "",
    self_id: str | None = None,
    master_ids=(),
    now: datetime | None = None,
    expand_quote: bool = True,
    expand_forward: bool = True,
    depth: int = 0,
):
    """把一条 NapCat 事件补全成 ``ChatMessage``。

    - 引用：只留被引用消息的一行摘要 + 图片 URL（图片不进渲染文本，供 OCR 工具解析）；
    - 转发：只有当前消息（depth=0）展开子消息，历史里的转发只留条数；
    - ``miniapp`` 显式传入时直接使用，否则从 json 卡片里自动识别。
    """
    cache = cache if cache is not None else {}
    resolved_self_id = str(self_id if self_id is not None else (mp.user_id or ""))

    mini_app = str(miniapp or "")
    if not mini_app:
        try:
            # 优先走 MessageProcessor 上的方法，保持旧版“可被子类 / 测试替换”的行为
            checker = getattr(mp, "check_is_miniapp", None)
            info = checker(event) if callable(checker) else check_is_miniapp(event)
            if info:
                title = str(info.get("title") or "").strip()
                desc = str(info.get("desc") or "").strip()
                mini_app = f"{title}｜{desc}" if title and desc else (title or desc)
        except Exception:
            mini_app = ""

    msg = parse_event(
        event,
        self_id=resolved_self_id,
        master_ids=master_ids,
        miniapp=mini_app,
        now=now,
    )

    reply_id = mp.is_reply(event)
    if reply_id and expand_quote:
        getter = getattr(mp, "_get_message_event", None)
        quoted = (
            await getter(reply_id, cache)
            if callable(getter)
            else await _get_message_event(mp, reply_id, cache)
        )
        if quoted:
            quoted_msg = parse_event(
                quoted, self_id=resolved_self_id, master_ids=master_ids, now=now
            )
            msg.quote = Quote(
                message_id=str(reply_id),
                sender=quoted_msg.sender,
                summary=message_summary(quoted_msg),
                images=quoted_msg.images,
            )

    if depth == 0:
        if expand_forward:
            fetcher = getattr(mp, "_fetch_forward_events", None)
            child_events, total = (
                await fetcher(event)
                if callable(fetcher)
                else await _fetch_forward_events(mp, event)
            )
            if child_events:
                msg.forward = [
                    await collect_message(
                        mp,
                        item,
                        self_id=resolved_self_id,
                        master_ids=master_ids,
                        now=now,
                        expand_quote=False,
                        expand_forward=False,
                        depth=depth + 1,
                        cache=cache,
                    )
                    for item in child_events
                ]
                msg.forward_total = total or len(msg.forward)
        elif msg.forward_total == 0:
            # 历史里的转发卡片不展开，只留条数
            try:
                if check_is_old_forward(event) or check_is_multimsg_forward(event):
                    msg.forward_total = _forward_count_from_card(event) or 1
            except Exception:
                pass
    return msg


async def build_chat_context(
    mp,
    event,
    *,
    history=None,
    history_count: int = 10,
    note: str = "",
    note_values: dict | None = None,
    master_ids=(),
    now: datetime | None = None,
    aliases=(),
) -> ChatContext:
    """组装本轮对话的完整上下文（场景 + 历史 + 当前消息）。

    - ``history`` 传 None 时自动拉取最近 ``history_count`` 条；传列表（ChatMessage）时直接用；
    - 历史接口是「包含当前这条」的，必须剔除，否则模型会以为它已经发生过了；
    - 临时会话历史（ThreadHistory）里更早的一段会并进来，缓存补上了更早的消息时放宽预算；
    - ``note`` 是 ``prompts.*_context`` 里的场景补充说明，旧默认模板会被忽略；
    - ``aliases`` 是 Bot 的别名（配置 ``interject.aliases``），用于判定「喊名字没 @」的召唤。
    """
    now = now or datetime.now()
    fetched = []
    if history is None:
        fetched = await collect_history(
            mp,
            event,
            count=history_count,
            reverse_order=True,
            **anchor_kwargs(event),
            master_ids=master_ids,
            now=now,
        )
        # 把临时会话历史里更早的那段补进来（一次爬十几条不够时靠它接住话头）
        buffered = mp.thread_history_store().snapshot(mp.thread_id_of(event))
        historical = merge_history(buffered, fetched)
    else:
        historical = list(history)

    note_text = str(note or "").strip()
    if note_text and is_legacy_context_note(note_text):
        note_text = ""  # 旧默认模板：群号/QQ/时间已有结构化字段，不再重复
    elif note_text and note_values:
        try:
            note_text = note_text.format(**note_values)
        except (KeyError, IndexError, ValueError):
            pass  # 用户写了别的占位符，原样当备注

    current_id = str(event.get("message_id") or "")
    # 历史接口是「包含当前这条」的，必须剔除，否则模型会以为它已经发生过了
    historical = [m for m in historical if m.message_id != current_id]

    current = await collect_message(mp, event, master_ids=master_ids, now=now)
    store = mp.thread_history_store() if history is None else None
    thread_id = mp.thread_id_of(event)
    if store is not None and current.quote is None:
        # 历史接口取不到被引用的那条（或已过期）→ 用缓存补上摘要，
        # 这样「对方是不是在回我」在 Agent 侧还能判出来
        reply_id = mp.is_reply(event)
        quoted = store.find(thread_id, reply_id) if reply_id else None
        if quoted is not None:
            current.quote = Quote(
                message_id=str(reply_id),
                sender=quoted.sender,
                summary=message_summary(quoted),
                images=quoted.images,
            )
    context = ChatContext(
        chat_type=str(event.get("message_type") or "group"),
        group_id=str(event.get("group_id") or ""),
        group_name=str(event.get("group_name") or ""),
        self_id=str(mp.user_id or event.get("self_id") or ""),
        master_ids=[str(x) for x in master_ids if str(x)],
        now=now,
        history=historical,
        current=current,
        note=note_text,
    )

    # 注意力等级在这里定一次：pipeline 用它决定进不进 AI，插话器用它决定让不让开。
    # quote 已经在上面补齐了，所以「引用我」在这里判得出来。
    attention = detect_attention(
        chat_type=context.chat_type,
        text=message_text(event),
        mentions_me=context.mentions_me,
        replies_to_me=context.replies_to_me,
        aliases=aliases,
    )
    context.attention = attention.level
    context.attention_evidence = attention.evidence

    if store is not None:
        # 缓存确实补上了更早的消息 → 放宽渲染预算，让这段接得上
        if len(historical) > len(fetched):
            context.max_history_messages = min(len(historical) + 1, 30)
            context.max_history_chars = max(context.max_history_chars, 3200)
        # 把这次拿到的高质量版本（带引用摘要）回写进缓存
        for message in historical:
            store.remember(thread_id, message)
        store.remember(thread_id, current)
    return context


__all__ = ["build_chat_context", "collect_message"]
