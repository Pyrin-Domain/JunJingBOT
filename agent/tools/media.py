"""图片相关工具：``ocr_img``。

图片来源由服务端从 ``ChatContext.image_sources()`` 解析，模型只传
``source + index + message_id``；长 URL 永远不会进入提示词。
"""

from __future__ import annotations

import logging
from typing import Any

from ..hints import _normalize_image_source, _urls_for_source
from .registry import (
    _MISSING,
    _current_images_var,
    get_cached_tool_result,
    media_of,
    record_tool_call,
    set_cached_tool_result,
    tool,
)

logger = logging.getLogger("QQBotAgent.Tools.Media")


async def _recognize(media: Any, url: str) -> dict:
    """兼容两种媒体接口：MediaService.ocr(url) / 旧 Extension.napcat_ocr(url)。"""
    func = getattr(media, "napcat_ocr", None)
    if func is None:
        func = getattr(media, "ocr", None)
    if func is None:
        raise RuntimeError("媒体服务不支持 OCR")
    result = func(url)
    if hasattr(result, "__await__"):
        result = await result
    return result if isinstance(result, dict) else {"text": str(result or "")}


def build_media_tools(agent: Any) -> list:
    @tool
    async def ocr_img(
        index: int = 0, source: str = "current", message_id: str = ""
    ) -> str:
        """对图片进行 OCR 文字识别，返回识别出的文字。
        当用户让你"识别图片"、"图片写了什么"、"OCR"时必须调用此工具。
        图片 URL 由服务端从上下文里解析，不需要也不要自己拼接 URL。
        参数:
          - index: int, 第几张图片（从 0 开始，默认 0）
          - source: str, 图片来源："current"=当前消息（默认），"quote"=被引用/回复的消息，
            "history"=历史消息
          - message_id: str, 仅 source="history" 时使用；不传则取历史上最近一条带图的消息
        """
        media = media_of(agent)
        sources = _current_images_var.get() or {}
        src = _normalize_image_source(source)
        mid = str(message_id or "").strip()
        urls = _urls_for_source(sources, src, mid)
        if not urls and src == "history" and mid:
            # 明确指定了某条历史消息却没图：说清楚，别拿别的图糊弄
            available = list((sources.get("history") or {}).keys())[-5:]
            ids = "、".join(reversed(available)) or "无"
            text = f"[这条历史消息没有可识别的图片；最近带图的消息 ID：{ids}]"
            logger.warning(f"[工具] ocr_img 历史消息 {mid} 没有图片")
            record_tool_call(
                "ocr_img",
                {"source": src, "index": index, "message_id": mid},
                result=text,
            )
            return text
        if not urls:
            other = "quote" if src == "current" else "current"
            if sources.get(other):
                logger.warning(
                    f"[工具] ocr_img source={source!r} 没有图片，回退到 {other}"
                )
                src = other
                urls = list(sources[other])
        if not urls:
            logger.warning("[工具] OCR 不可用 - 当前消息、引用消息和历史里都没有图片")
            record_tool_call(
                "ocr_img",
                {"source": src, "index": index, "message_id": mid},
                result="[OCR 不可用：这一轮没有可识别的图片]",
            )
            return "[OCR 不可用：这一轮没有可识别的图片]"
        try:
            idx = int(index)
        except (TypeError, ValueError):
            idx = 0
        if idx < 0 or idx >= len(urls):
            logger.warning(
                f"[工具] ocr_img source={src} index={index} 越界（共 {len(urls)} 张），按第 0 张处理"
            )
            idx = 0
        img_url = urls[idx]
        logger.info(
            f"[工具调用] ocr_img(source={src}, index={index} -> {idx}, message_id={mid or '-'}, img_url={img_url[:80]}...)"
        )
        cached = get_cached_tool_result("ocr_img", {"img_url": img_url})
        if cached is not _MISSING:
            logger.info(f"[工具缓存] 复用 ocr_img 结果: {img_url[:80]}...")
            record_tool_call(
                "ocr_img", {"source": src, "index": idx, "message_id": mid}, result=cached
            )
            return cached
        if media is None:
            logger.warning("[工具] OCR 不可用 - extension 为空")
            record_tool_call(
                "ocr_img", {"source": src, "index": idx, "message_id": mid}, result="[OCR 不可用]"
            )
            return "[OCR 不可用]"
        try:
            result = await _recognize(media, img_url)
            text = result.get("text", "") or "[未识别到文字]"
            logger.info(
                f"[工具结果] ocr_img -> {text[:80]}{'...' if len(text)>80 else ''}"
            )
            set_cached_tool_result("ocr_img", {"img_url": img_url}, text)
            record_tool_call(
                "ocr_img", {"source": src, "index": idx, "message_id": mid}, result=text
            )
            return text
        except Exception as e:
            logger.error(f"[工具异常] ocr_img 失败: {e}")
            record_tool_call(
                "ocr_img", {"source": src, "index": idx, "message_id": mid}, error=str(e)
            )
            return f"[OCR 失败: {e}]"

    return [ocr_img]


__all__ = ["build_media_tools"]
