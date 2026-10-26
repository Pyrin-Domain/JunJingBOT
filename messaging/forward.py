"""转发卡片 / 合并转发 / 小程序卡片的解析与展开。

旧代码把这块混在 ``NapCatTools.py`` 里，算法本身与 ``forward_dedup.py`` 的
卡片解析完全一样（去重库也用它），所以这里保留一份实现，供 messaging 与
``bot.services.dedup`` 共同 import。

约定：需要发网络请求的函数把 :class:`messaging.processor.MessageProcessor`
作为第一个参数传入（``mp``），避免模块级循环 import。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from .models import render_forward, render_message


# ══════════════════════════════════════════════════════════
# 卡片解析（从旧 forward_dedup.py 搬来）
# ══════════════════════════════════════════════════════════
def _loads(text, default=None):
    """json.loads 的安全版：NapCat 的字段时有时无、时而是字符串时而是对象，
    解不出来就当没有。"""
    try:
        return json.loads(text)
    except Exception:
        return default


def iter_json_payloads(msg_event: dict) -> Iterator[dict]:
    """依次 yield 消息里 json 卡片解析出来的 dict。"""
    for seg in (msg_event or {}).get("message", []) or []:
        if not isinstance(seg, dict) or seg.get("type") != "json":
            continue
        raw = (seg.get("data") or {}).get("data")
        if not raw:
            continue
        payload = _loads(raw)
        if isinstance(payload, dict):
            yield payload


def get_miniapp_info(msg_event: dict) -> dict:
    """解析小程序卡片（appid / title / desc / link / preview / url），与转发者无关。"""
    for payload in iter_json_payloads(msg_event):
        if payload.get("app") != "com.tencent.miniapp_01":
            continue
        meta = payload.get("meta")
        detail = meta.get("detail_1") if isinstance(meta, dict) else None
        # title/desc 缺一个就当作结构不认识（不同版本 NapCat 的字段不一样）
        if not isinstance(detail, dict) or "title" not in detail or "desc" not in detail:
            continue
        title, desc = detail["title"], detail["desc"]
        return {
            "title": title,
            "desc": desc,
            "link": detail.get("qqdocurl") or detail.get("url") or "",
            "appid": str(detail.get("appid") or ""),
            "preview": detail.get("preview") or "",
            "url": detail.get("url") or "",
            "context": f"[title:{title}][desc:{desc}]",
        }
    return {}


def get_multimsg_detail(msg_event: dict) -> dict:
    """解析新版合并转发卡片（com.tencent.multimsg）的 meta.detail。"""
    for payload in iter_json_payloads(msg_event):
        if payload.get("app") != "com.tencent.multimsg":
            continue
        detail = ((payload.get("meta") or {}).get("detail")) or {}
        if not isinstance(detail, dict):
            continue
        raw_extra = payload.get("extra")
        extra = _loads(raw_extra) if isinstance(raw_extra, str) else None
        return {**detail, "_extra": extra if isinstance(extra, dict) else {}}
    return {}


def get_multimsg_resid(msg_event: dict) -> str | None:
    """新版合并转发的 resid（取子消息要用它，不是 message_id）。"""
    detail = get_multimsg_detail(msg_event)
    resid = detail.get("resid")
    return str(resid) if resid else None


# ══════════════════════════════════════════════════════════
# 类型判定
# ══════════════════════════════════════════════════════════
def check_is_miniapp(msg_event: dict) -> dict:
    """检测小程序卡片，返回 title/desc/link/context（另含 appid/preview/url）。"""
    return get_miniapp_info(msg_event)


def check_is_multimsg_forward(msg_event: dict) -> str | None:
    """检测是否为新版合并转发 JSON 卡片；是则返回 resid，否则 None。"""
    return get_multimsg_resid(msg_event)


def check_is_old_forward(msg_event: dict) -> bool:
    """确定是不是旧版 forward（message 里直接有 type=forward 的段）。"""
    for seg in (msg_event or {}).get("message", []) or []:
        if isinstance(seg, dict) and seg.get("type") == "forward":
            return True
    return False


# ══════════════════════════════════════════════════════════
# 取消息 / 展开转发
# ══════════════════════════════════════════════════════════
async def _get_message_event(mp, message_id, cache: dict | None = None):
    """按消息 ID 取原始事件；同一轮内缓存，历史里互相引用时不会重复发包。"""
    key = str(message_id)
    cache = cache if cache is not None else {}
    if key in cache:
        return cache[key]
    data = None
    try:
        await mp.ensure_connected()
        res = await mp.nc.get_message(message_id=message_id)
        if res.get("status") != "failed":
            data = res.get("data") or None
    except Exception as e:
        print(f"[上下文] 取消息 {message_id} 失败: {e}")
    cache[key] = data
    return data


def _forward_count_from_card(event) -> int:
    """从转发卡片的文案里读原始条数（拿不到就返回 0）。"""
    try:
        raw = json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    match = re.search(r"查看(\d+)条转发消息", raw) or re.search(r"(\d+)条转发", raw)
    return int(match.group(1)) if match else 0


async def _fetch_forward_events(mp, event) -> tuple[list[dict], int]:
    """取转发聊天记录的子消息，返回 ``(事件列表, 原始条数)``。"""
    try:
        if check_is_old_forward(event):
            payload = {"message_id": event.get("message_id")}
        else:
            rid = check_is_multimsg_forward(event)
            if not rid:
                return [], 0
            payload = {"message_id": rid}
    except Exception:
        return [], 0

    try:
        await mp.ensure_connected()
        res = await mp.nc.get_forward_msg(payload)
    except Exception as e:
        print(f"[上下文] 取转发消息失败: {e}")
        return [], 0
    if res.get("status") == "failed":
        print(f"[上下文] 取转发消息失败: {res.get('message')}")
        return [], 0

    messages = (res.get("data") or {}).get("messages") or []
    normalized: list[dict] = []
    for item in messages:
        # 部分版本把子消息放在 content 里，统一成 message 段
        if not item.get("message") and item.get("content"):
            item = {**item, "message": item["content"]}
        normalized.append(item)
    # 接口有时会截断，卡片上写的条数更准
    total = max(len(normalized), _forward_count_from_card(event))
    return normalized[:40], total


async def get_forward_try(mp, msg_event: dict) -> str:
    """把一条（可能已被展开的）转发事件逐条渲染成文本。"""
    res = ""
    for msg in (msg_event or {}).get("message", []) or []:
        if not isinstance(msg, dict):
            continue
        for seg in (msg.get("data", {}) or {}).get("content", []) or []:
            res += await generate_structed_message_to_creat_context(mp, seg)
    return res


async def generate_structed_message_to_creat_context(mp, event, isOCR: bool = False) -> str:
    """兼容旧调用：单条消息渲染成一行结构化文本（引用/转发仍会补全）。

    新代码请用 ``collect_message`` / ``build_chat_context``，能得到完整结构体。
    """
    msg = await mp.collect_message(event, self_id=mp.user_id)
    text = render_message(msg, now=msg.time)
    if msg.forward:
        text += "\n" + "\n".join(render_forward(msg, {}, now=msg.time))
    if isOCR:
        for url in msg.images:
            ocr_res = await mp.ocr_text(url)
            if not ocr_res:
                continue
            text += f"\n[OCR] {ocr_res}"
    return text


__all__ = [
    "check_is_miniapp",
    "check_is_multimsg_forward",
    "check_is_old_forward",
    "generate_structed_message_to_creat_context",
    "get_forward_try",
    "get_miniapp_info",
    "get_multimsg_detail",
    "get_multimsg_resid",
    "iter_json_payloads",
]
