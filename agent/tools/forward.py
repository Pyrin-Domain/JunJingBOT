"""合并转发 / 聊天记录卡片解析：``read_forward``。

QQ 的「合并转发」在上下文里只是一张壳：转发段被 :func:`parse_event` 直接跳过，
正文只剩一行 ``【转发聊天记录 N 条】``。模型看到这行既读不到里面的字，也拿不到
里面的图，于是只能回一句「内容读不出来呀，主人重发一次？」
（现场见 ``logs/agent/2026-11-02.json``）。

这个工具负责把壳撬开：

1. **定位卡片**——引用 > 当前 > 历史（主人常常隔几分钟才回头问「总结一下那个
   聊天记录」，那时卡片已经沉到历史里了）；
2. **取候选 forward id**——``forward`` 段 id / 新版卡片 resid / ``raw_message``
   里的 CQ 码 / 卡片自身 message id，挨个试，第一个能取回内容的就用；
3. **摊平**——交给 :class:`AbstractionLayer.ForwardExpander`。NapCat 的嵌套层
   只能靠内联 ``content``，SnowLuma 的必须拿内层 id 再发包，这段差异写在抽象层
   的能力表里，上层不认后端名；
4. **交出图片**——卡片里的图片 URL 登记到 ``source="forward"``，``ocr_img``
   随后就能读卡内的图（URL 仍然只留在服务端，不进提示词）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from AbstractionLayer import ForwardExpander
from AbstractionLayer.forward import ForwardEntry
from AbstractionLayer.segments import STICKER, image_kind
from messaging.forward import get_multimsg_resid

from .registry import (
    _MISSING,
    _current_context_var,
    _current_images_var,
    api_of,
    get_cached_tool_result,
    record_tool_call,
    set_cached_tool_result,
    tool,
)

logger = logging.getLogger("QQBotAgent.Tools.Forward")

#: 最多渲染多少条卡片内容（再多的对「总结一下」没意义）。
MAX_RENDER_ITEMS = 60
#: 单条正文最多多少字。
MAX_ITEM_CHARS = 200

_CQ_FORWARD = re.compile(r"\[CQ:forward,id=([^\],]+)")

_WHERE_LABEL = {
    "quote": "被引用的消息",
    "current": "当前消息",
    "history": "历史消息",
    "指定": "指定的消息",
}


# ══════════════════════════════════════════════════════════
# 定位
# ══════════════════════════════════════════════════════════
def _locate_card() -> dict | None:
    """在本轮上下文里找一张转发卡片；找不到返回 None。

    优先级：引用 > 当前 > 历史（新的在前）——主人问「总结一下这个聊天记录」时，
    说的通常是他刚刚回复过的那张卡。
    """
    context = _current_context_var.get()
    if context is None:
        return None
    getter = getattr(context, "forward_candidates", None)
    if not callable(getter):
        return None
    try:
        candidates = getter() or []
    except Exception as exc:  # noqa: BLE001 - 定位失败不该连累主流程
        logger.warning(f"[工具] 定位转发卡片失败: {exc}")
        return None
    return candidates[0] if candidates else None


# ══════════════════════════════════════════════════════════
# 取候选 id
# ══════════════════════════════════════════════════════════
def _candidate_ids(event: dict, card_message_id: str) -> list[str]:
    """从卡片事件里收集所有可能的 forward id，按可信度排序。

    实测（SnowLuma）：``get_msg`` 返回的 ``message`` 段里直接带 ``forward.id``，
    而 ``raw_message`` 里的 CQ id 多了 ``/808`` 后缀——**去掉后缀反而取不到**，
    所以 CQ 码要原样保留。
    """
    out: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    for seg in (event or {}).get("message") or []:
        if not isinstance(seg, dict) or seg.get("type") != "forward":
            continue
        data = seg.get("data") or {}
        _add(data.get("id"))
        _add(data.get("resid"))

    try:
        _add(get_multimsg_resid(event))
    except Exception:  # noqa: BLE001 - 不是新版卡片就当没有
        pass

    match = _CQ_FORWARD.search(str((event or {}).get("raw_message") or ""))
    if match:
        _add(match.group(1))

    # 卡片自身的 message id：NapCat / SnowLuma 都能直接用它当转发 id 查。
    _add(card_message_id)
    return out


# ══════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════
def _leaf_text(message: dict) -> tuple[str, list[str]]:
    """把卡片里的一条消息渲染成 ``名字：正文``，顺带收集它的图片 URL。"""
    sender = message.get("sender") or {}
    name = str(
        sender.get("card") or sender.get("nickname") or sender.get("user_id") or "未知"
    )

    segments = message.get("message") or message.get("content") or []
    parts: list[str] = []
    urls: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append(f"@{data.get('qq')}")
        elif kind == "image":
            url = str(data.get("url") or "")
            if url:
                urls.append(url)
            # 收藏表情（sticker）也是 image 段，但它没有画面文字可读
            parts.append("[表情]" if image_kind(seg) == STICKER else "[图片]")
        elif kind in ("face", "mface", "marketface"):
            parts.append("[表情]")
        elif kind == "forward":
            parts.append("[嵌套转发]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "file":
            parts.append(f"[文件 {data.get('name') or ''}]".strip())
        elif kind == "json":
            parts.append("[卡片]")
        elif kind == "reply":
            continue

    body = "".join(parts).strip() or "[无正文]"
    if len(body) > MAX_ITEM_CHARS:
        body = body[:MAX_ITEM_CHARS] + "…"

    stamp = message.get("time")
    prefix = ""
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            from datetime import datetime

            prefix = datetime.fromtimestamp(stamp).strftime("%H:%M ") + ""
        except (OverflowError, OSError, ValueError):
            prefix = ""
    return f"{prefix}{name}：{body}", urls


def _render(entries: list, expected: int) -> tuple[str, list[str]]:
    """渲染展开后的叶子消息；返回 (文本, 图片 URL 列表)。"""
    lines: list[str] = []
    urls: list[str] = []
    shown = 0
    for entry in entries:
        if shown >= MAX_RENDER_ITEMS:
            break
        message = getattr(entry, "message", None)
        if not isinstance(message, dict):
            continue
        text, imgs = _leaf_text(message)
        urls.extend(imgs)
        lines.append(f"{shown + 1}. {text}")
        shown += 1

    total = expected or len(entries)
    head = f"合并转发聊天记录共 {total} 条"
    if shown < len(entries):
        head += f"（下面只列出前 {shown} 条）"
    if not lines:
        return head + "，但没有解析出正文。", urls
    return head + "：\n" + "\n".join(lines), urls


# ══════════════════════════════════════════════════════════
# 图片登记
# ══════════════════════════════════════════════════════════
def _register_forward_images(urls: list[str]) -> None:
    """把卡片里的图片登记成 ``source="forward"``，供 ``ocr_img`` 按 index 取。

    **必须原地改，不能重新赋值**：langchain 的 ``BaseTool.arun`` 用
    ``coro_with_context`` 在复制出来的 context 里跑工具，工具里
    ``_current_images_var.set(...)`` 的新值传不回调用方（实测：``ainvoke``
    之后外面读到的还是旧值）。原地改同一个 dict 对象才看得见——这跟
    ``registry.record_tool_call`` 靠 ``calls.append(...)`` 上报是同一个道理。

    字典本身是 :meth:`ChatContext.image_sources` 每轮新建的那一个，所以
    原地改也不会跨轮次泄漏。
    """
    if not urls:
        return
    sources = _current_images_var.get()
    if not isinstance(sources, dict):
        sources = {}
        _current_images_var.set(sources)
    sources["forward"] = list(urls)


# ══════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════
def build_forward_tools(agent: Any) -> list:
    @tool
    async def read_forward(message_id: str = "") -> str:
        """解析合并转发 / 聊天记录卡片（QQ 的「转发」消息），返回里面的真实内容。

        对方让你「总结一下这个聊天记录」「这个转发里说了什么」「看看那个记录」
        时**必须**调用此工具——上下文里只有一行【转发聊天记录 N 条】，内容读不出来。

        参数:
          - message_id: str, 要解析的卡片消息 ID；**不传就自动定位**本轮上下文里
            最近的一张转发卡片（引用 > 当前 > 历史）

        返回的是卡片里的正文（按条列出）。如果卡片里有图片，返回里会写明有几张，
        要读图上的字再调用 ocr_img(source="forward", index=0)。
        """
        mid = str(message_id or "").strip()
        where = "指定"
        expected = 0

        if mid:
            card = {"message_id": mid, "where": where, "count": 0}
        else:
            card = _locate_card()
            if not card:
                text = (
                    "[这一轮上下文里没有找到合并转发/聊天记录卡片；"
                    "如果对方确实发过一张，请用 read_forward(message_id=卡片消息ID) 指定]"
                )
                logger.warning("[工具] read_forward 上下文里没有转发卡片")
                record_tool_call("read_forward", {"message_id": ""}, result=text)
                return text
            mid = str(card.get("message_id") or "")
            where = str(card.get("where") or "指定")
            expected = int(card.get("count") or 0)

        cached = get_cached_tool_result("read_forward", {"message_id": mid})
        if cached is not _MISSING:
            logger.info(f"[工具缓存] 复用 read_forward 结果: {mid}")
            record_tool_call("read_forward", {"message_id": mid}, result=cached)
            return cached

        api = api_of(agent)
        if api is None:
            text = "[解析转发失败：没有可用的协议接口]"
            logger.warning("[工具] read_forward 没有 api")
            record_tool_call("read_forward", {"message_id": mid}, error=text)
            return text

        # ① 取卡片原始事件（拿 forward id 用）
        event: dict = {}
        try:
            raw = await api.get_message(mid)
            if isinstance(raw, dict):
                data = raw.get("data")
                if isinstance(data, dict):
                    event = data
        except Exception as exc:  # noqa: BLE001 - 取不到就靠候选 id 硬试
            logger.warning(f"[工具] read_forward 取卡片事件失败({exc})，改用候选 id 探测")

        # ② 挨个试候选 id
        candidates = _candidate_ids(event, mid)
        if not candidates:
            candidates = [mid]

        entries: list = []
        errors: list[str] = []
        expander = None
        kernel = getattr(api, "kernel", None)
        if kernel is not None:
            expander = ForwardExpander(kernel)

        for candidate in candidates:
            try:
                if expander is not None:
                    expansion = await expander.expand(candidate, expected=expected)
                    if expansion.entries:
                        entries = list(expansion.entries)
                        expected = expected or expansion.expected
                        break
                    errors.extend(expansion.errors)
                else:
                    # 没有内核就退化成单层查询（拿不到嵌套，但比什么都不做强）
                    res = await api.get_forward_msg({"message_id": candidate})
                    messages = ((res or {}).get("data") or {}).get("messages") or []
                    if messages:
                        entries = [
                            ForwardEntry(message=item, depth=1, via="fetch")
                            for item in messages
                            if isinstance(item, dict)
                        ]
                        break
            except Exception as exc:  # noqa: BLE001 - 换下一个候选继续试
                errors.append(f"{candidate}: {exc}")

        if not entries:
            detail = errors[-1] if errors else "没有候选 id 能取到内容"
            text = (
                f"[解析转发失败：这张卡片取不到内容（{detail}）。"
                "卡片可能已过期，或者它不是合并转发而是普通分享链接。"
                "你可以如实告诉对方读不出来，别编内容。]"
            )
            logger.warning(f"[工具] read_forward 解析失败: {detail}")
            record_tool_call("read_forward", {"message_id": mid}, error=text)
            return text

        # ③ 渲染 + 登记图片
        body, urls = _render(entries, expected)
        _register_forward_images(urls)

        label = _WHERE_LABEL.get(where, where)
        header = f"（来自{label} {mid}）"
        if urls:
            body += (
                f"\n卡片里有 {len(urls)} 张图片；"
                f'要读图上的字，调用 ocr_img(source="forward", index=0..{len(urls) - 1})。'
            )
        text = f"{body}\n{header}"

        logger.info(
            f"[工具结果] read_forward({mid}) -> {len(entries)} 条，{len(urls)} 张图"
        )
        set_cached_tool_result("read_forward", {"message_id": mid}, text)
        record_tool_call("read_forward", {"message_id": mid}, result=text)
        return text

    return [read_forward]


__all__ = ["build_forward_tools"]
