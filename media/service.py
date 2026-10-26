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
from pathlib import Path
from typing import Any

from download_system.providers.bilibili import BilibiliProvider

# images 模块级函数仅作为兜底；正常路径走注入的 resolver。
from images import to_napcat as _module_to_napcat

from . import image_hash as image_hash_module
from .ocr import OCR, get_default_ocr


class MediaService:
    def __init__(self, api, images, downloads, settings, *, ocr: OCR | None = None):
        self.api = api
        self.images = images
        self.downloads = downloads
        self.settings = settings
        self._ocr = ocr or get_default_ocr()

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

    async def _send_message(self, event, message) -> bool:
        if not event:
            return False
        message_type = event.get("message_type")
        if message_type == "group":
            await self.api.send_group_message(
                group_id=event.get("group_id"), message=message
            )
            return True
        if message_type == "private":
            await self.api.send_private_message(
                user_id=event.get("user_id"), message=message
            )
            return True
        return False

    async def send_video_file(self, event, path, *, summary: str = "") -> bool:
        """发送本地视频文件；路径必须先经过 images 转成 NapCat 可读形式。"""
        if not path:
            return False
        napcat_path = self._to_napcat(path)
        data: dict[str, Any] = {"file": napcat_path}
        if summary:
            data["summary"] = summary
        return await self._send_message(
            event, [{"type": "file", "data": data}]
        )

    # ── B 站 ─────────────────────────────────────────────────────
    async def send_bili_video(self, event, url: str) -> bool:
        """下载 B 站视频并发送；失败时回退发送下载地址文本（旧行为）。"""
        if not url:
            return False
        try:
            result = await self.downloads.download_bilibili(url)
            path = self._result_attr(result, "path")
            if not path:
                raise RuntimeError("下载结果没有本地文件")
            if await self.send_video_file(event, path):
                return True
            raise RuntimeError("发送本地视频失败")
        except Exception as exc:
            print(f"[B站卡片] 视频发送失败，回退下载地址: {exc}")
            try:
                await self._send_message(event, f"下载地址：{url}")
            except Exception:
                pass
            return False

    async def send_bili_card(self, event, ark_data: dict) -> bool:
        """发送 B 站小程序卡片。"""
        if not ark_data:
            return False
        try:
            return await self._send_message(
                event, [{"type": "json", "data": ark_data}]
            )
        except Exception as exc:
            print(f"[B站卡片] 卡片发送失败: {exc}")
            return False

    async def get_bili_ark(
        self, tittle: str, desc: str, picUrl: str, jumpUrl: str
    ) -> dict:
        """获取 B 站 ark 卡片数据（保留旧参数名）。"""
        params = {
            "type": "bili",
            "title": tittle,
            "desc": desc,
            "picUrl": picUrl,
            "jumpUrl": jumpUrl,
        }
        print(f"[B站卡片] 生成卡片: {params}")
        return await self.api.get_mini_app_ark(params=params)

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


__all__ = ["MediaService"]
