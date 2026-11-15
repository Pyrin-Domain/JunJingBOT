"""``junjingtest_sex_filter``：图片过滤门的在线测试命令。

用法：**回复一条聊天记录**（或直接发命令，自动找本群往上数最近的一条聊天记录）
发送 ``junjingtest_sex_filter``，Bot 对记录里的图把注册表里的过滤函数**全部跑一遍**
（不看 ``enable`` 开关——测试命令要的就是全量判定），把结果回在当前群。

和自动搬运的过滤门（``image_filter`` → 审计群打标签）完全独立：

* 只判定、只报告，**不拦截、不发审计群、不写任何库**；
* 复用同一个 :class:`~messaging.image_filter.ImageFilterHub`（注册表 / 下载缓存 /
  最近命令追踪），所以看到的结果就是搬运时会判出来的结果；
* 每张图除了命中情况，还会附 NSFW 原始打分（模型可用时），方便校准阈值。
"""

from __future__ import annotations

import asyncio

from bot.services.dedup import get_multimsg_detail, get_multimsg_resid
from core.logging import get_logger
from messaging.image_filter import (
    ImageProbe,
    nsfw_score_bytes,
    probes_from_event,
    probes_from_messages,
)

__all__ = ["sex_filter_test"]

log = get_logger("bot.commands.sex_filter_test")

#: 不回复时往上翻多少条历史找聊天记录
_HISTORY_SCAN_COUNT = 30
#: 单条报告最多列多少张图（再多只报总数）
_MAX_REPORT_ROWS = 15


# ════════════════════ 定位目标 ════════════════════
async def _get_message(api, message_id) -> dict:
    """按 id 取单条消息事件；拿不到返回空 dict。"""
    if api is None or not message_id:
        return {}
    try:
        res = await api.get_message(message_id)
    except Exception as exc:
        log.warning("get_msg(%s) 失败: %s", message_id, exc)
        return {}
    data = (res or {}).get("data")
    return data if isinstance(data, dict) and data.get("message") else {}


def _is_record_event(event: dict) -> bool:
    """这条消息是不是聊天记录卡片（旧版 forward / 新版 multimsg）。"""
    segs = (event or {}).get("message") or []
    if any(seg.get("type") == "forward" for seg in segs if isinstance(seg, dict)):
        return True
    return bool(get_multimsg_detail(event))


def _has_image(event: dict) -> bool:
    segs = (event or {}).get("message") or []
    return any(seg.get("type") == "image" for seg in segs if isinstance(seg, dict))


async def _locate_record(handler, event) -> tuple[dict, str]:
    """找要判定的目标：回复的消息 > 往上数最近的一条聊天记录。

    :return: ``(消息事件, 来源描述)``；找不到时第一个为空 dict
    """
    mp, api = handler.mp, handler.api
    reply_id = mp.is_reply(event)
    if reply_id:
        msg = await _get_message(api, reply_id)
        if msg:
            return msg, f"回复的消息 {reply_id}"
        # get_msg 拿不到（可能已过期）也把 id 交给内容抓取试一把
        return {"message_id": reply_id}, f"回复的消息 {reply_id}（卡片内容待取）"

    # 没回复：从本群最近历史里往上数，找最近的一条聊天记录
    group_id = event.get("group_id")
    if api is None or not group_id:
        return {}, ""
    try:
        res = await api.get_group_msg_history(
            group_id, count=_HISTORY_SCAN_COUNT, reverse_order=False
        )
    except Exception as exc:
        log.warning("取群历史失败: %s", exc)
        return {}, ""
    messages = ((res or {}).get("data") or {}).get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or str(msg.get("message_id")) == str(
            event.get("message_id")
        ):
            continue
        if _is_record_event(msg):
            return msg, f"往上数最近的一条聊天记录（message_id={msg.get('message_id')}）"
    return {}, ""


async def _fetch_messages(handler, record_event: dict) -> tuple[list, str]:
    """取聊天记录内容；取不到且目标本身带图时按单条消息处理。

    **优先走抽象层展开**（``ForwardExpander``）：SnowLuma 的聊天记录经常是
    嵌套的 —— 单层 ``get_forward_msg`` 取回来全是内层 ``forward id``，一张
    图片都看不到（搬运的去重路径也是这么处理的）。
    """
    api = handler.api
    if api is None:
        return [], "没有 API"

    # ① 抽象层展平：嵌套记录递归展开成叶子消息
    kernel = getattr(api, "kernel", None)
    if kernel is not None:
        try:
            from AbstractionLayer import ForwardExpander

            expander = ForwardExpander(kernel)
            resid = get_multimsg_resid(record_event)
            expansion = (
                await expander.expand(resid)
                if resid
                else await expander.expand_event(record_event)
            )
        except Exception as exc:  # noqa: BLE001 - 展开失败退回单层
            log.warning("抽象层展开异常（%s），退回单层取内容", exc)
        else:
            if expansion.messages:
                return list(expansion.messages), f"expand[{expansion.describe()}]"
            if expansion.errors:
                log.warning(
                    "抽象层没展开出内容（%s），退回单层取内容",
                    "；".join(expansion.errors),
                )

    # ② 单层 get_forward_msg（NapCat 内联 / 无嵌套记录够用）
    candidates: list = []
    resid = get_multimsg_resid(record_event)
    if resid:
        candidates.append(resid)
    if record_event.get("message_id"):
        candidates.append(record_event["message_id"])
    for seg in record_event.get("message") or []:
        if isinstance(seg, dict) and seg.get("type") == "forward":
            candidates.append((seg.get("data") or {}).get("id"))

    seen: set = set()
    for message_id in candidates:
        key = str(message_id or "")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            await handler.mp.ensure_connected()
            res = await api.get_forward_msg({"message_id": message_id})
        except Exception as exc:
            log.warning("get_forward_msg(%s) 异常: %s", key, exc)
            continue
        status = str((res or {}).get("status") or "none")
        messages = ((res or {}).get("data") or {}).get("messages") or []
        if status != "failed" and messages:
            return list(messages), f"get_forward_msg[{key}]"

    # ③ 目标本身带图（回复的是单张图，不是记录）
    if _has_image(record_event):
        return [record_event], "目标本身带图（单条消息）"
    return [], f"取不到内容（展开 + {len(seen)} 个 id 的单层查询都失败）"


# ════════════════════ 判定 + 报告 ════════════════════
def _hub_of(handler):
    """复用搬运链路的 hub；ForwardService 不在时退回独立 hub。"""
    forward = getattr(handler, "forward", None)
    gate = getattr(forward, "image_gate", None)
    hub = getattr(gate, "hub", None)
    if hub is not None:
        return hub
    from messaging.image_filter import ImageFilterHub

    return ImageFilterHub(settings=handler.settings)


def _filter_params(hub) -> dict[str, dict]:
    """从配置里抄每个过滤 id 的 params（**不看 enable**，测试要全量）。"""
    params: dict[str, dict] = {}
    for entry in hub.config.get("filters") or []:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        filter_id = str(entry.get("id") or entry.get("filter_id") or "").strip()
        if filter_id:
            params[filter_id] = entry.get("params") if isinstance(entry.get("params"), dict) else {}
    return params


def _sender_label(probe: ImageProbe) -> str:
    name = str(probe.sender_name or "").strip()
    uid = str(probe.sender_id or "").strip()
    if name and uid:
        return f"{name}（{uid}）"
    return name or uid or "未知"


def _run_filters(hub, probes: list[ImageProbe]) -> tuple[dict, set]:
    """全量跑注册表里的过滤函数。

    :return: ``({probe.key: [hit, ...]}, 判定异常的 filter_id 集合)``
    """
    params_by_id = _filter_params(hub)
    results: dict = {}
    broken: set = set()
    for probe in probes:
        text, age = hub.commands.recent(probe.group_id)
        probe.context.setdefault("recent_text", text)
        probe.context.setdefault("recent_age", age)
        hits = []
        for spec in hub.registry.all():
            try:
                hit = spec.match(probe, params_by_id.get(spec.filter_id, {}))
            except Exception as exc:
                log.warning("%s 判定异常: %s", spec.filter_id, exc)
                broken.add(spec.filter_id)
                continue
            if hit is not None:
                hits.append(hit)
        if hits:
            results[probe.url or f"{probe.message_id}:{probe.index}"] = hits
    return results, broken


def _verdict_text(hits: list, nsfw: dict | None) -> str:
    """一张图的结论文案：命中 > NSFW 打分 > 模型不可用。"""
    if hits:
        return "；".join(
            f"{hit.label}（{hit.filter_id}）{hit.detail}".rstrip() for hit in hits
        )
    if nsfw is not None:
        wording = {"nsfw": "是色图", "review": "疑似色图（需人工确认）"}.get(
            nsfw["verdict"], "不是色图"
        )
        return f"{wording}（nsfw={nsfw['score']} {nsfw['verdict']}）"
    return "未命中（nsfw 模型不可用，无原始分）"


def _build_report(
    hub,
    probes: list[ImageProbe],
    hits_by_key: dict,
    broken: set,
    *,
    total_images: int,
    source: str,
    diag: str,
    max_images: int = 0,
) -> tuple[str, list[tuple[ImageProbe, str]]]:
    """生成报告文本 + 每张已判定图的 ``(probe, 结论文案)`` 回显清单。"""
    lines = [f"【图片过滤测试】来源：{source}"]
    if diag:
        lines.append(f"取内容：{diag}")
    names = " / ".join(spec.filter_id for spec in hub.registry.all())
    status = "、".join(sorted(broken)) or "全部正常"
    lines.append(f"过滤器：{names}（{status}）")
    echo: list[tuple[ImageProbe, str]] = []
    if not probes:
        lines.append("记录里没有可下载判定的图片")
        return "\n".join(lines), echo

    params_by_id = _filter_params(hub)
    nsfw_ready = False
    shown = 0
    failed = 0
    hit_count = 0
    for probe in probes:
        key = probe.url or f"{probe.message_id}:{probe.index}"
        hits = hits_by_key.get(key) or []
        nsfw = None
        if probe.data:
            nsfw = nsfw_score_bytes(probe.data, params_by_id.get("nsfw") or {})
            if nsfw is not None:
                nsfw_ready = True
        verdict = _verdict_text(hits, nsfw)
        if probe.data:
            echo.append((probe, verdict))
        row = f"#{probe.index + 1} {_sender_label(probe)}：{verdict}"
        if probe.data is None and probe.url:
            failed += 1
            reason = f"（{probe.error}）" if probe.error else ""
            row = f"#{probe.index + 1} {_sender_label(probe)}：图片下载失败{reason}，未判定"
            verdict = None
        if hits:
            hit_count += 1
        if shown < _MAX_REPORT_ROWS:
            lines.append(row)
            shown += 1
    if len(probes) > shown:
        lines.append(f"…其余 {len(probes) - shown} 张未逐条列出")
    tail = f"共 {total_images} 张图（判定前 {len(probes)} 张，上限 {max_images or '不限'}）"
    if failed:
        tail += f"，下载失败 {failed} 张"
    if total_images > len(probes):
        tail += "，超出上限的未判定（把 max_images 设为 0 可全量判定）"
    tail += f"，命中 {hit_count} 张"
    lines.append(tail)
    if params_by_id.get("nsfw") and not nsfw_ready:
        lines.append("（NSFW 模型不可用，未出原始分）")
    return "\n".join(lines), echo


# ════════════════════ 命令入口 ════════════════════
async def sex_filter_test(handler, event) -> None:
    """``junjingtest_sex_filter``：判定回复的聊天记录 / 往上数最近的聊天记录。

    整体兜底：任何一步抛异常都把错误回进群里 + 打印完整 traceback——
    测试命令绝不能「无声失败」。
    """
    try:
        await _run_test(handler, event)
    except Exception as exc:  # noqa: BLE001 - 兜底反馈
        log.exception("图片过滤测试命令执行异常")
        await _reply(
            handler,
            event,
            f"【图片过滤测试】命令执行出错：{exc.__class__.__name__}: {exc}"
            "（完整 traceback 见 Bot 控制台）",
        )


async def _run_test(handler, event) -> None:
    mp, api = handler.mp, handler.api
    record_event, source = await _locate_record(handler, event)
    if not record_event:
        await _reply(handler, event, "用法：回复一条聊天记录发送 junjingtest_sex_filter；"
                                     "或直接发送，我会找本群往上数最近的一条聊天记录。")
        return

    messages, diag = await _fetch_messages(handler, record_event)
    hub = _hub_of(handler)
    config = hub.config
    # 判定上限：0（配置默认）= 全量，测试命令永远是「把记录里的图全判一遍」，
    # 否则它会用和搬运一样的漏判方式给出「全部正常」的假结论。
    max_images = int(config.get("max_images") or 0)

    if not messages:
        await _reply(handler, event, f"【图片过滤测试】取不到聊天记录内容（{diag}）")
        return

    probes = probes_from_messages(messages, event=record_event, max_images=max_images)
    total_images = sum(
        1
        for msg in messages
        for seg in (msg.get("message") or [])
        if isinstance(seg, dict) and seg.get("type") == "image"
    )
    if not probes:
        await _reply(
            handler,
            event,
            f"【图片过滤测试】{source}\n记录里没有图片，没什么可判定的（{diag}）",
        )
        return
    # 判定用的大小上限放宽到 30MB（生产 max_image_bytes 默认 20MB）——之前"下载失败"的
    # 大图全是 9~25MB 的 GIF，全量下下来 NSFW / 版式都能正常判
    await hub.download_probes(probes, max_bytes=30 * 1024 * 1024)
    hits_by_key, broken = await asyncio.to_thread(_run_filters, hub, probes)
    report, echo = _build_report(
        hub,
        probes,
        hits_by_key,
        broken,
        total_images=total_images,
        source=source,
        diag=diag,
        max_images=max_images,
    )
    log.info("%s", report.replace("\n", " | "))
    await _reply(handler, event, report)
    await _send_echo_images(handler, event, echo)


# ════════════════════ 回显回调：[{text}，{image}] 逐张发 ════════════════════
#: 单条回显消息的图片大小上限（base64 前），和 image_gate 的标签一致
_MAX_ECHO_IMAGE_BYTES = 6 * 1024 * 1024
#: 两条回显之间的间隔，避免连发触发风控
_ECHO_INTERVAL = 0.5


def _echo_image_segment(probe: ImageProbe) -> dict | None:
    import base64

    data = probe.data
    if data and len(data) <= _MAX_ECHO_IMAGE_BYTES:
        return {
            "type": "image",
            "data": {
                "file": "base64://" + base64.b64encode(data).decode("ascii"),
                "summary": "过滤判定",
            },
        }
    if probe.url:
        return {"type": "image", "data": {"file": probe.url, "summary": "过滤判定"}}
    return None


async def _send_echo_images(handler, event, echo: list[tuple[ImageProbe, str]]) -> int:
    """判定回调：每张判定过的图发一条 ``[{text}，{image}]``，文本写明是不是色图。"""
    sent = 0
    for probe, verdict in echo:
        segment = _echo_image_segment(probe)
        if segment is None:
            continue
        message = [
            {"type": "text", "data": {"text": f"#{probe.index + 1} {_sender_label(probe)}：{verdict}"}},
            segment,
        ]
        try:
            await handler.mp.ensure_connected()
            if event.get("message_type") == "private":
                await handler.api.send_private_message(
                    user_id=event["user_id"], message=message
                )
            else:
                await handler.api.send_group_message(
                    group_id=event["group_id"], message=message
                )
            sent += 1
        except Exception as exc:
            log.warning("回显 #%s 发送失败: %s", probe.index + 1, exc)
        await asyncio.sleep(_ECHO_INTERVAL)
    if echo:
        log.info("判定回显已发送 %s/%s 张", sent, len(echo))
    return sent


async def _reply(handler, event, text: str) -> None:
    """把报告回在命令所在会话，引用命令消息。"""
    message = [
        {"type": "reply", "data": {"id": str(event.get("message_id") or "")}},
        {"type": "text", "data": {"text": text}},
    ]
    try:
        await handler.mp.ensure_connected()
        if event.get("message_type") == "private":
            await handler.api.send_private_message(
                user_id=event["user_id"], message=message
            )
        else:
            await handler.api.send_group_message(
                group_id=event["group_id"], message=message
            )
    except Exception as exc:
        log.exception("发送报告失败: %s", exc)
        log.info("%s", text)
