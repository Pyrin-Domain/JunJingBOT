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

__all__ = ["jm", "parse_jm_arg", "JM_ARG_RE", "GROUP_HINT", "NOT_READY", "DISABLED"]

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

#: 「协议端明确拒绝」的重试退避（秒）。QQ 的 -134 是间歇性的，
#: 实测 0.2~0.4 秒就返回，退避着再试几次成功率明显更高。
RETRY_DELAYS = (2.0, 10.0, 30.0)

#: 直接当图片发的后缀；其它（pdf / zip / 视频）走「文件」通道
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def _resp_error(resp) -> str:
    """协议端明确失败时返回错误描述；成功返回空串。

    ``transport.call_api`` 只做 echo 配对，后端回
    ``{"status": "failed", "retcode": 100, "wording": "...-134..."}`` **不会抛异常**，
    不自己检查就会出现「文件没发出去但命令当成功」。
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
        print(f"[jm] 回执失败: {exc}")


async def _post_file(handler, event, path: Path) -> dict:
    """发一次，原样返回协议端响应（异常不吞）。"""
    msg_type = str(event.get("message_type") or "")
    api = getattr(handler, "api", None)
    if api is None or not hasattr(api, "send_private_message"):
        raise RuntimeError("协议端 API 不可用")
    kind = "image" if path.suffix.lower() in IMAGE_SUFFIXES else "file"
    segment = {"type": kind, "data": {"file": _to_napcat(handler, path)}}
    await handler.mp.ensure_connected()
    if msg_type == "group":
        return await api.send_group_message(
            group_id=event.get("group_id"), message=[segment], timeout=SEND_TIMEOUT
        )
    return await api.send_private_message(
        user_id=event.get("user_id"), message=[segment], timeout=SEND_TIMEOUT
    )


async def _send_private_file(handler, event, path: Path) -> str:
    """发文件；成功返回空串，失败返回原因（调用方负责回执）。

    只对**协议端明确拒绝**重试（QQ 的 -134 就是这一类，实测间歇出现、
    0.2~0.4 秒就返回，退避重试命中率明显更高）。

    **超时不重试**：超时说明协议端还在传那个文件，重试只会多传一份。
    """
    attempt = 0
    while True:
        try:
            resp = await _post_file(handler, event, path)
        except TimeoutError as exc:
            return f"等待响应超时（{exc}）；文件可能仍在后台传输，请稍后确认"
        error = _resp_error(resp)
        if not error:
            return ""
        if attempt >= len(RETRY_DELAYS):
            return error
        delay = RETRY_DELAYS[attempt]
        attempt += 1
        print(f"[jm] 发送被拒（{error}），{delay:g} 秒后第 {attempt} 次重试")
        await asyncio.sleep(delay)


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
        print(f"[jm] 发送 {target.name}（{target.stat().st_size / 1048576:.1f} MB）")
        try:
            error = await _send_private_file(handler, event, target)
        except Exception as exc:  # noqa: BLE001 - 发送失败只回执，不打崩任务
            print(f"[jm] 文件发送异常: {exc}")
            await _reply(handler, event, f"jm 发送失败：{exc}")
            return False
        if error:
            print(f"[jm] 文件发送失败: {error}")
            text = f"jm 发送失败：{error}"
            if "-134" in error:
                # QQ 对这个文件的拒绝是间歇性的，重试过后还失败就等一会儿再来
                text += "（QQ 暂时拒绝上传文件，过一会儿再试）"
            await _reply(handler, event, text)
            return False
        return True

    meta = getattr(result, "meta", None) or {}
    await _reply(handler, event, f"jm 完成：{path or meta}")
    return True
