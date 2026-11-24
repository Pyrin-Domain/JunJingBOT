"""媒体能力粘合层：OCR、图片哈希、B 站视频 / 卡片、点歌。

对应旧 ``Minitor/NapCatTools.py`` 里的 B 站与媒体方法。本模块只做编排：

* 下载交给 ``download_system.DownloadService``；
* 本地路径交给 ``images.ImageResolver.to_napcat``，绝不手写 ``/mnt`` 或盘符；
* 发送交给注入的 NapCat API（``send_group_message`` / ``send_private_message`` ...）。

``MediaService`` 不 import ``messaging`` / ``bot`` / ``agent``，消息类型完全从
事件字典里读，保持单向依赖。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from download_system.providers.bilibili import BilibiliProvider

# images 模块级函数仅作为兜底；正常路径走注入的 resolver。
from images import to_napcat as _module_to_napcat

from AbstractionLayer.kernal import GENERIC, BackendCapabilities

from . import image_hash as image_hash_module
from .bili_history import DEFAULT_KEEP, BiliHistory, video_key
from .ocr import OCR, get_default_ocr

#: 发文件 / 视频段时等协议端回执的秒数。
#:
#: 协议端要等**整个上传结束**才回包，30 秒的全局默认值必然不够：
#: ``bot/commands/jm.py`` 实测 8MB≈32s、10MB≈66s、36MB≈149s，B 站视频（几十到
#: 上百 MB）更慢。这里给足 10 分钟，可用 ``bot_config.json`` 的
#: ``media.send_timeout`` 覆盖。
DEFAULT_SEND_TIMEOUT = 600.0


class MediaService:
    def __init__(
        self,
        api,
        images,
        downloads,
        settings,
        *,
        ocr: OCR | None = None,
        capabilities: BackendCapabilities | None = None,
    ):
        self.api = api
        self.images = images
        self.downloads = downloads
        self.settings = settings
        self._ocr = ocr or get_default_ocr()
        self._caps = capabilities
        self._history: BiliHistory | None = None

    @property
    def capabilities(self) -> BackendCapabilities:
        """发送通道策略来自协议内核**实测**出来的能力，不在这里写死后端名字。

        优先用注入的（测试 / 精简调用）；否则向 ``api.kernel`` 要；都没有就按
        最保守的通用档案走。
        """
        if self._caps is not None:
            return self._caps
        caps = getattr(getattr(self.api, "kernel", None), "capabilities", None)
        return caps if caps is not None else GENERIC

    # ── OCR ──────────────────────────────────────────────────────
    async def ocr(self, url: str, re_ocr: bool = False) -> dict:
        return await self._ocr.recognize(url, re_ocr=re_ocr)

    async def ocr_and_send(self, group_id, url):
        result = await self.ocr(url)
        text = str((result or {}).get("text") or "")
        await self.api.send_group_message(group_id=group_id, message=text)
        return result

    # ── 图片哈希 ─────────────────────────────────────────────────
    async def hash_remote_images(self, urls: list[str]) -> list[str]:
        return await image_hash_module.hash_remote_images(urls)

    def get_url(self, event) -> list[str]:
        urls: list[str] = []
        for item in (event or {}).get("message", []) or []:
            if item.get("type", "") != "image":
                continue
            url = (item.get("data") or {}).get("url")
            if not url:
                continue
            urls.append(url)
        return urls

    # ── 发送辅助 ─────────────────────────────────────────────────
    @staticmethod
    def _result_attr(result, key: str, default=None):
        if isinstance(result, dict):
            return result.get(key, default)
        return getattr(result, key, default)

    def _to_napcat(self, path: str | Path) -> str:
        converter = getattr(self.images, "to_napcat", None)
        if callable(converter):
            return converter(str(path))
        return _module_to_napcat(str(path))

    def _send_timeout(self) -> float:
        """发文件 / 视频等回执的秒数（配置 ``media.send_timeout``，默认 600）。"""
        getter = getattr(self.settings, "get", None)
        raw = getter("media.send_timeout", None) if callable(getter) else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_SEND_TIMEOUT
        return value if value > 0 else DEFAULT_SEND_TIMEOUT

    @staticmethod
    def _is_timeout(exc) -> bool:
        """超时 vs 「协议端明确拒绝」：两者处理方式相反。

        超时说明**上传还在进行**（回执慢），重试只会多传一份；``-134`` 之类
        是明确拒绝，可以退避重试。
        """
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError))

    async def _send_message(self, event, message, *, timeout: float | None = None) -> bool:
        if not event:
            return False
        extra = {} if timeout is None else {"timeout": timeout}
        message_type = event.get("message_type")
        if message_type == "group":
            await self.api.send_group_message(
                group_id=event.get("group_id"), message=message, **extra
            )
            return True
        if message_type == "private":
            await self.api.send_private_message(
                user_id=event.get("user_id"), message=message, **extra
            )
            return True
        return False

    async def send_video_file(self, event, path, *, summary: str = "") -> bool:
        """发送本地视频文件；路径必须先经过 images 转成协议端可读形式。

        为什么不是「发一次 file 段就完事」：QQ 的**群文件**上传会偶发
        ``retcode=-134 文件上传失败``（同一文件过几秒重发就成功）。旧代码不校验
        ``retcode``，把失败当成功，于是用户什么也收不到，也不会走到发链接的兜底。

        通道顺序、重试次数、重试间隔、视频段体积上限**全部来自
        :class:`BackendCapabilities`**（``media_send_order`` / ``media_retry_times``
        / ``media_retry_delay`` / ``video_max_bytes``）。默认顺序是
        ``file → video``：群文件走文件上传通道，视频消息走 NTV2 群视频通道，
        两套配额互不影响，一套被禁时另一套通常还能用。

        超时（:meth:`_send_timeout`，默认 600 秒）**既不重试也不换通道**：回执慢
        通常意味着协议端还在传，再发一份就是重复。全失败则抛异常，调用方
        :meth:`send_bili_video` 只打日志、不回复任何文案。
        """
        if not path:
            return False

        caps = self.capabilities
        napcat_path = self._to_napcat(path)
        file_data: dict[str, Any] = {"file": napcat_path}
        if summary:
            file_data["summary"] = summary
        size = self._file_size(path)

        last_error: Exception | None = None
        for channel in caps.media_send_order or ():
            if channel == "file":
                ok, last_error = await self._send_file_channel(event, file_data, caps)
            elif channel == "video":
                if size and caps.video_max_bytes and size > caps.video_max_bytes:
                    print(
                        f"[视频] 文件 {size / 1024 / 1024:.1f}MB 超过视频消息上限"
                        f"（{caps.video_max_bytes / 1024 / 1024:.0f}MB），跳过该通道"
                    )
                    continue
                print("[视频] 改发视频消息（群视频通道）")
                ok, last_error = await self._send_segment(
                    event, "video", {"file": napcat_path}, label="视频消息"
                )
            else:
                continue
            if ok:
                return True
            if self._is_timeout(last_error):
                print(
                    f"[视频] {self._send_timeout():g} 秒没回执，按「可能已发出」收工："
                    "不重试、不换通道（再发一份就是重复）"
                )
                return False

        if last_error is not None:
            raise last_error
        return False

    async def _send_file_channel(self, event, data, caps):
        """``file`` 段：按能力表重试（-134 多为瞬时风控）。"""
        attempts = max(1, int(caps.media_retry_times) + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            ok, last_error = await self._send_segment(
                event, "file", data, label=f"群文件（第 {attempt}/{attempts} 次）"
            )
            if ok:
                return True, None
            if self._is_timeout(last_error):
                # 上传还没回执，重试会重复上传
                return False, last_error
            if attempt < attempts and caps.media_retry_delay:
                await asyncio.sleep(caps.media_retry_delay)
        return False, last_error

    async def _send_segment(self, event, seg_type: str, data: dict, *, label: str):
        """发一个段；不抛异常，返回 ``(是否成功, 最后一个错误)``。"""
        try:
            await self._send_message(
                event,
                [{"type": seg_type, "data": data}],
                timeout=self._send_timeout(),
            )
            return True, None
        except Exception as exc:  # noqa: BLE001 - 失败要换通道，不在这里中断
            print(f"[视频] {label}失败: {exc}")
            return False, exc

    @staticmethod
    def _file_size(path) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    # ── B 站 ─────────────────────────────────────────────────────
    def _bili_history(self) -> BiliHistory | None:
        """下载历史表（``data/bili_history.json``）。

        关掉（``media.bili_history=false``）或拿不到数据目录（精简调用 / 测试）时
        返回 ``None``，调用方按「没有历史」走，不影响发送。
        """
        if self._history is not None:
            return self._history
        getter = getattr(self.settings, "get", None)
        if callable(getter) and not getter("media.bili_history", True):
            return None
        paths = getattr(self.settings, "paths", None)
        data_dir = getattr(paths, "data_dir", None)
        if data_dir is None:
            return None
        self._history = BiliHistory(
            Path(data_dir) / "bili_history.json", keep=DEFAULT_KEEP
        )
        return self._history

    async def send_bili_video(self, event, url: str) -> bool:
        """下载 B 站视频并发送；同一条视频（BV 号）只下载一次。

        下载历史见 :class:`media.bili_history.BiliHistory`：命中就直接发本地
        文件，跳过 ``download_bilibili``（同一个视频会被不同群 / 回复卡片反复
        触发，重下既慢又占盘）。**先落库再发送**，发送超时也不算白下。

        失败**只打日志、不回复任何文案**：旧版超时后会把解析出来的直链当
        「下载地址」发出去，那条链接带时效签名 / 常是分段地址，用户看到的基本
        都是错的，所以整条回退分支已删除（用户要求「超时就什么都不做」）。
        """
        if not url:
            return False

        key = video_key(url)
        history = self._bili_history()
        cached = history.get(key) if history is not None else None
        if cached is not None:
            print(f"[B站卡片] 命中下载历史 {key}，跳过下载: {cached.name}")
            try:
                return await self.send_video_file(event, cached)
            except Exception as exc:  # noqa: BLE001 - 与下面同：失败静默
                kind = "发送超时" if self._is_timeout(exc) else "视频发送失败"
                print(f"[B站卡片] {kind}（历史文件 {key}），不做回复: {exc}")
                return False

        try:
            result = await self.downloads.download_bilibili(url)
            path = self._result_attr(result, "path")
            if not path:
                raise RuntimeError("下载结果没有本地文件")
            if history is not None:
                meta = self._result_attr(result, "meta", None) or {}
                history.remember(key, path, title=str(meta.get("title") or ""))
            return await self.send_video_file(event, path)
        except Exception as exc:  # noqa: BLE001 - 失败静默，不回复文案
            kind = "发送超时" if self._is_timeout(exc) else "视频发送失败"
            print(f"[B站卡片] {kind}，不做回复: {exc}")
            return False

    async def send_bili_card(self, event, ark_data: dict) -> bool:
        """发送 B 站**小程序**卡片。

        ``ark_data`` 必须是 ``get_bili_ark`` 返回的**卡片本体**（``{"ver":...}``），
        不是 OneBot 的整封响应。而且**一个字都不能改**：``config.token`` 是服务端
        对该卡内容的签名，改了 prompt / desc / preview 任意一个，QQ 会**静默丢弃**
        （SnowLuma 表现为 ``retcode=100 未收到回执``，历史里查不到），
        详见 ``docs/ABSTRACTION.md``。
        """
        if not ark_data:
            return False
        payload = ark_data if isinstance(ark_data, str) else json.dumps(
            ark_data, ensure_ascii=False
        )
        try:
            return await self._send_message(
                event, [{"type": "json", "data": {"data": payload}}]
            )
        except Exception as exc:
            print(f"[B站卡片] 卡片发送失败: {exc}")
            return False

    async def send_bili_news_card(
        self,
        event,
        *,
        title: str,
        jump_url: str,
        desc: str = "",
        preview: str = "",
        summary: str = "[分享]",
    ) -> bool:
        """发送 B 站**图文卡**（``com.tencent.tuwen.lua``，可点击的图文卡）。

        为什么必须走后端动作、不能手拼 JSON：实测（2026-11-15）个人号发送手写的
        ``com.tencent.structmsg`` / 手搓 ``tuwen.lua`` / 官方 ark 23-24-37 模板，
        QQ 一律把它**降级成纯文本**「发送者版本过低，无法展示内容」——卡的
        ``config.token`` 得由服务器签，本地造不出来（SnowLuma 与 NapCat 表现一致）。
        所以这里调 :attr:`BackendCapabilities.tuwen_ark_action` 指定的动作：
        SnowLuma 的 ``send_tuwen_ark`` 内部走 NTQQ 原生接口，能出真卡；NapCat
        没有这个动作，直接返回 False，让调用方回退到别的形态。

        与**小程序卡**的分工：小程序卡（:meth:`get_bili_ark` + :meth:`send_bili_card`）
        样式是 B 站原生卡、点开进 b 站页面；图文卡样式朴素但一定能渲染出来。
        """
        if not title or not jump_url:
            return False
        action = getattr(self.capabilities, "tuwen_ark_action", None)
        if not action:
            print("[B站卡片] 当前后端没有图文卡动作（tuwen_ark_action 为空），跳过")
            return False
        if event.get("message_type") != "group":
            # 这个动作只认 group_id，私聊没有对应参数。
            return False
        params = {
            "group_id": event.get("group_id"),
            "title": title,
            "desc": desc or "B站视频",
            "summary": summary,
            "jump_url": jump_url,
        }
        if preview:
            # 不传就用后端默认图；传空串会把默认图也顶掉。
            params["preview_url"] = preview
        try:
            await self.api.call_api(action=action, params=params)
            return True
        except Exception as exc:
            print(f"[B站卡片] 图文卡发送失败: {exc}")
            return False

    async def get_bili_ark(
        self, tittle: str, desc: str, picUrl: str, jumpUrl: str, **extra
    ) -> dict:
        """获取 B 站小程序 ark 卡片**本体**（保留旧参数名）。

        ``jumpUrl`` 的语义在 QQ 那边是「小程序页面路径」，不是网页地址：
        发视频要传 ``/pages/video/video?bvid=BV...``。传成 https 链接的话，
        卡片能发出去、点开却落在 b 站首页（``url`` 短链里没记住页面）。

        ``extra`` 透传 SnowLuma 扩展参数（``version_id`` / ``web_url`` / ``scene``），
        NapCat 侧不认这些键，传了也会被忽略，不影响主流程。

        返回值已经从 OneBot 响应信封（``{"status":..,"data":{"data":..}}``）里剥出来；
        ``_call_api`` 不剥 ``data``，旧代码直接把整封信封当卡片发，是个死链。
        """
        params = {
            "type": "bili",
            "title": tittle,
            "desc": desc,
            "picUrl": picUrl,
            "jumpUrl": jumpUrl,
        }
        params.update(extra)
        print(f"[B站卡片] 生成卡片: {params}")
        result = await self.api.get_mini_app_ark(params=params) or {}
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data if isinstance(data, dict) else {}

    async def get_bili_ark_from_text(self, text: str):
        """从文本中提取第一个 B 站视频并生成卡片数据。"""
        if not text:
            return None
        video_infos = await asyncio.to_thread(
            BilibiliProvider.get_video_info_from_text, text
        )
        if not video_infos:
            return None
        video_info = video_infos[0]
        return await self.get_bili_ark(
            "君景解析:" + str(video_info.get("title") or ""),
            "B站视频",
            str(video_info.get("cover_url") or ""),
            str(video_info.get("url") or ""),
        )

    async def get_bili_video_download_url(self, text: str) -> str | None:
        """从文本中解析出第一个 B 站视频的可下载地址。"""
        urls = BilibiliProvider.extract_video_urls(text)
        print(f"[B站卡片] 提取到的 B 站 URL 列表: {urls}")
        for url in urls:
            try:
                resolved = await asyncio.to_thread(
                    BilibiliProvider.resolve_video_url, url
                )
                print(f"[B站卡片] 原始 URL -> 解析后 URL: {url} -> {resolved}")
                download_url = await asyncio.to_thread(
                    BilibiliProvider.get_video_download_url, url
                )
                print(f"[B站卡片] 最终下载地址: {download_url}")
                return download_url
            except Exception as exc:
                print(f"[B站卡片] 解析失败: url={url}, error={exc!r}")
                continue
        print(f"[B站卡片] 未能从文本中解析出任何 B 站下载地址: {text}")
        return None

    # ── 点歌 ─────────────────────────────────────────────────────
    async def send_music(self, event, keyword: str) -> bool:
        """点歌：只发送远程 ``music_url``，不下载音频。"""
        try:
            result = await self.downloads.download_music(keyword)
        except Exception as exc:
            print(f"[点歌] 搜索失败: {exc}")
            return False

        meta = self._result_attr(result, "meta", None) or {}
        music_url = str(meta.get("music_url") or "")
        # 旧 GetMusic 固定发群、且 record 段用 ``url`` 字段；保持原样。
        try:
            await self.api.send_group_message(
                group_id=(event or {}).get("group_id"),
                message=[{"type": "record", "data": {"url": music_url}}],
            )
            return True
        except Exception as exc:
            print(f"[点歌] 发送失败: {exc}")
            return False


__all__ = ["MediaService", "DEFAULT_SEND_TIMEOUT"]
