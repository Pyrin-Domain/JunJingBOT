"""``/jm <id>``：占位命令。

行为边界（刻意写死，避免以后被顺手放开）：

* **只响应私聊**。群里发 ``/jm`` 只回一句提示，不下载、不发送任何文件；
* 不做抓取。真正的取文件逻辑在 ``download_system.providers.jm.JMProvider``，
  那里目前是显式占位（``JmNotConfigured``）；
* 失败一律回文字，不抛异常到主流程。

用法：

    /jm <作品标识>      # 私聊
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from AbstractionLayer.kernal import GENERIC
from core.logging import get_logger
from transport.api import OneBotAPIError

__all__ = ["jm", "parse_jm_arg", "JM_ARG_RE", "GROUP_HINT", "NOT_READY", "DISABLED"]

log = get_logger("bot.commands.jm")

# 只在前缀位置匹配，避免正文里出现的 "/jm" 误触发
JM_ARG_RE = re.compile(r"^\s*/jm(?:\s+(?P<arg>\S+))?\s*$", re.I)

GROUP_HINT = "jm 只在私聊可用"
USAGE_HINT = "用法：/jm <作品标识>"
DISABLED = "jm 命令未启用（config: jm.enable=false）"
NOT_READY = "jm 占位图不可用"
SEND_FAILED = "jm 结果发送超时[>=30s]"

#: 发文件时等响应的秒数。协议端要等**整个上传结束**才回包：
#: 实测 8MB≈32s、10MB≈66s、36MB≈149s，默认 30 秒必然不够。
SEND_TIMEOUT = 300.0

#: 闪传上传的等待时间。闪传是另一套上传服务，大文件（几百 MB）比群文件更慢，
#: 这里不跟 ``SEND_TIMEOUT`` 共用，免得「传完了但回执还没到」被误判成失败。
FLASH_TIMEOUT = 600.0

#: 「协议端明确拒绝」的重试退避（秒）。-134 的根因（file 段缺 name）已在
#: 抽象层修掉；这里保留给限频 / 配额等真正的瞬时拒绝。
RETRY_DELAYS = (2.0, 10.0, 30.0)

#: 直接当图片发的后缀；其它（pdf / zip / 视频）走「文件」通道
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def _resp_error(resp) -> str:
    """协议端明确失败时返回错误描述；成功返回空串。

    ``OneBotAPI`` 对 ``send_*_msg`` 会先过 :func:`transport.api.ensure_ok`，
    明确失败直接抛 :class:`OneBotAPIError`；这里只兜底「没走严格校验」的
    后端 / 测试替身，避免「文件没发出去但命令当成功」。
    """
    if not isinstance(resp, dict):
        return ""
    status = str(resp.get("status") or "").lower()
    retcode = resp.get("retcode", 0)
    if status in ("", "ok", "async") and retcode in (0, None):
        return ""
    detail = resp.get("wording") or resp.get("message") or resp.get("msg") or ""
    return f"retcode={retcode} {detail}".strip()


def is_enabled(handler) -> bool:
    """读 ``bot_config.json`` 的 ``jm.enable``；缺失时按关闭处理。"""
    settings = getattr(handler, "settings", None)
    if isinstance(settings, dict):
        return bool((settings.get("jm") or {}).get("enable", False))
    getter = getattr(settings, "get", None)
    if callable(getter):
        return bool(getter("jm.enable", False))
    return False


def parse_jm_arg(text: str) -> str | None:
    """从清洗后的文本里取出参数；没写参数返回 ``None``。"""
    match = JM_ARG_RE.match(str(text or ""))
    if not match:
        return None
    return (match.group("arg") or "") or None


def _to_napcat(handler, path) -> str:
    """本地路径转 NapCat 可读形式；转换层缺失时原样返回。"""
    images = getattr(getattr(handler, "mp", None), "images", None)
    converter = getattr(images, "to_napcat", None)
    if callable(converter):
        return converter(str(path))
    try:
        from images import to_napcat as module_to_napcat

        return module_to_napcat(str(path))
    except Exception:  # noqa: BLE001 - 没配 resolver 时退回原始路径
        return str(path)


async def _reply(handler, event, text: str) -> None:
    try:
        await handler.mp.send_message(event=event, message=text)
    except Exception as exc:  # noqa: BLE001 - 回执失败不该打崩任务
        log.warning("回执失败: %s", exc)


async def _post_file(handler, event, path: Path) -> dict:
    """发一次，原样返回协议端响应（异常不吞）。"""
    msg_type = str(event.get("message_type") or "")
    api = getattr(handler, "api", None)
    if api is None or not hasattr(api, "send_private_message"):
        raise RuntimeError("协议端 API 不可用")
    kind = "image" if path.suffix.lower() in IMAGE_SUFFIXES else "file"
    # file 段的 name 由抽象层统一补（AbstractionLayer.segments.normalize_outgoing）：
    # SnowLuma 缺 name 会被 QQ 以 -134 拒绝，NapCat 则把 name 当可选显示名。
    segment = {"type": kind, "data": {"file": _to_napcat(handler, path)}}
    await handler.mp.ensure_connected()
    if msg_type == "group":
        return await api.send_group_message(
            group_id=event.get("group_id"), message=[segment], timeout=SEND_TIMEOUT
        )
    return await api.send_private_message(
        user_id=event.get("user_id"), message=[segment], timeout=SEND_TIMEOUT
    )


def _capabilities(handler):
    """当前协议端的发送能力；拿不到时按最保守的 ``GENERIC`` 走。"""
    api = getattr(handler, "api", None)
    caps = getattr(getattr(api, "kernel", None), "capabilities", None)
    return caps if caps is not None else GENERIC


def _nested(node, path: str):
    """按点分路径取嵌套字段；取不到返回 ``None``。"""
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _flash_inner_error(resp) -> str:
    """检查闪传响应 ``data`` 里的内核级失败码。

    SnowLuma 失败会直接回 ``status/retcode`` 失败；NapCat 的
    ``create_flash_task`` 把内核结果原样透传，外层 ``retcode`` 仍是 0，
    这时要看 ``data.result`` / ``data.errCode``。
    """
    if not isinstance(resp, dict) or not isinstance(resp.get("data"), dict):
        return ""
    data = resp["data"]
    result = data.get("result")
    if isinstance(result, int) and result != 0:
        return str(data.get("errMsg") or data.get("message") or f"result={result}")
    err = data.get("errCode")
    if isinstance(err, int) and err != 0:
        return str(data.get("errMsg") or f"errCode={err}")
    return ""


async def _send_file_channel(handler, event, path: Path) -> tuple[str, bool]:
    """群文件 / 私聊文件通道；返回 ``(错误描述, 是否超时)``。

    只对**协议端明确拒绝**重试（限频 / 配额等瞬时失败）。明确拒绝在真实
    ``OneBotAPI`` 上是 :class:`OneBotAPIError`（``send_group_msg`` /
    ``send_private_msg`` 在 ``strict_actions`` 里），不是返回失败 dict ——
    漏掉这个分支会让整段重试变成死代码。``-134`` 的根因（``file`` 段缺
    ``name``）已由抽象层统一补上，不再依赖重试碰运气。

    **超时不重试**：超时说明协议端还在传那个文件，重试只会多传一份。
    """
    attempt = 0
    while True:
        try:
            resp = await _post_file(handler, event, path)
        except TimeoutError as exc:
            return f"等待响应超时（{exc}）；文件可能仍在后台传输，请稍后确认", True
        except OneBotAPIError as exc:
            error = str(exc)
        else:
            error = _resp_error(resp)
        if not error:
            return "", False
        if attempt >= len(RETRY_DELAYS):
            return error, False
        delay = RETRY_DELAYS[attempt]
        attempt += 1
        log.warning("发送被拒（%s），%g 秒后第 %s 次重试", error, delay, attempt)
        await asyncio.sleep(delay)


async def _send_flash_channel(handler, event, path: Path) -> str:
    """闪传通道：``create_flash_task`` 上传 + ``send_flash_msg`` 发卡片。

    闪传走 QQ 的独立文件集服务，和群文件上传配额互不影响，是 ``-134``
    之后唯一还能把 PDF 送出去的链路。成功返回空串，失败返回原因。
    """
    api = getattr(handler, "api", None)
    create = getattr(api, "create_flash_task", None)
    send = getattr(api, "send_flash_msg", None)
    if not callable(create) or not callable(send):
        return "协议端不支持闪传（缺少 create_flash_task / send_flash_msg）"

    caps = _capabilities(handler)
    try:
        resp = await create(
            [_to_napcat(handler, path)], name=path.name, timeout=FLASH_TIMEOUT
        )
        error = _flash_inner_error(resp)
        data = resp.get("data") if isinstance(resp, dict) else None
        field = getattr(caps, "flash_fileset_path", "") or "fileset_id"
        fileset_id = "" if error else _nested(data, field)
        if not error and not fileset_id:
            error = "闪传没有返回 fileset_id"
        if error:
            return f"闪传失败：{error}"

        if str(event.get("message_type") or "") == "group":
            target = {"group_id": event.get("group_id")}
        else:
            target = {"user_id": event.get("user_id")}
        resp = await send(str(fileset_id), timeout=FLASH_TIMEOUT, **target)
        error = _flash_inner_error(resp)
        return f"闪传卡片发送失败：{error}" if error else ""
    except TimeoutError as exc:
        return f"闪传等待响应超时（{exc}）"
    except OneBotAPIError as exc:
        return f"闪传失败：{exc}"


async def _send_private_file(handler, event, path: Path) -> str:
    """发文件；成功返回空串，失败返回原因（调用方负责回执）。

    通道顺序来自 ``BackendCapabilities.file_send_order``：先走文件通道
    （带退避重试），失败再走闪传。图片没有对应的闪传语义，只走文件通道。
    文件通道**超时就收工**：协议端可能还在传，换通道会发两份。
    """
    caps = _capabilities(handler)
    channels = getattr(caps, "file_send_order", None) or ("file",)
    if path.suffix.lower() in IMAGE_SUFFIXES:
        channels = ("file",)

    errors: list[str] = []
    for channel in channels:
        if channel == "file":
            error, timed_out = await _send_file_channel(handler, event, path)
            if not error:
                return ""
            errors.append(error)
            if timed_out:
                return error
        elif channel == "flash":
            log.info("文件通道失败，改发闪传（独立上传服务）")
            error = await _send_flash_channel(handler, event, path)
            if not error:
                return ""
            errors.append(error)

    return "；".join(errors) if errors else "没有可用的发送通道"


async def jm(handler, event):
    """``/jm`` 的入口：私聊闸门 → 解析参数 → 交给下载池 → 回执。"""
    # 1) 私聊闸门：群聊直接挡掉，连解析都不做
    # if str(event.get("message_type") or "") != "private":
    #     await _reply(handler, event, GROUP_HINT)
    #     return False

    clean_text = handler.mp.get_clean_context(event=event)
    arg = parse_jm_arg(clean_text)
    if not arg:
        await _reply(handler, event, USAGE_HINT)
        return False

    if not is_enabled(handler):
        await _reply(handler, event, DISABLED)
        return False

    downloads = getattr(handler, "downloads", None)
    download_jm = getattr(downloads, "download_jm", None)
    if not callable(download_jm):
        await _reply(handler, event, NOT_READY)
        return False

    try:
        result = await download_jm(arg)
    except NotImplementedError:
        # provider 占位：这是当前预期路径
        await _reply(handler, event, NOT_READY)
        return False
    except Exception as exc:  # noqa: BLE001 - 下载失败只回执
        await _reply(handler, event, f"jm 下载失败：{exc}")
        return False

    path = getattr(result, "path", None)
    if path and Path(path).is_file():
        target = Path(path)
        log.info("发送 %s（%.1f MB）", target.name, target.stat().st_size / 1048576)
        try:
            error = await _send_private_file(handler, event, target)
        except Exception as exc:  # noqa: BLE001 - 发送失败只回执，不打崩任务
            log.exception("文件发送异常: %s", target)
            await _reply(handler, event, f"jm 发送失败：{exc}")
            return False
        if error:
            log.warning("文件发送失败: %s", error)
            text = f"jm 发送失败：{error}"
            if "-134" in error:
                # 带 name 后不应再出现；真出现就按限频 / 配额处理，让用户稍后再试
                text += "（QQ 暂时拒绝上传文件，过一会儿再试）"
            await _reply(handler, event, text)
            return False
        return True

    meta = getattr(result, "meta", None) or {}
    await _reply(handler, event, f"jm 完成：{path or meta}")
    return True
