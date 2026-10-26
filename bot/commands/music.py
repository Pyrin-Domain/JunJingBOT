"""``get_music``：点歌。

旧实现直接调 ``MusicServer.search_and_resolve_first_song`` 同步解析，然后发一条
``record`` 段（远程 URL），不下载文件。新实现把这部分交给
``media.send_music(event, keyword)``；没有注入 media 时退回旧路径：解析出
``music_url`` 后直接发 record（用 ``download_system.providers.music`` 的同一个解析函数）。
"""

from __future__ import annotations

import asyncio
import re

__all__ = ["get_music", "GetMusic"]


async def _send_record(handler, event, url: str) -> None:
    await handler.mp.ensure_connected()
    await handler.api.send_group_message(
        group_id=event.get("group_id"),
        message=[{"type": "record", "data": {"url": url}}],
    )


async def _fallback_music(handler, event, query: str) -> None:
    """media 不可用时的退路：与旧行为一致，只发远程 record URL。"""
    try:
        from download_system.providers.music import search_and_resolve_first_song

        res = await asyncio.to_thread(search_and_resolve_first_song, query)
    except Exception as exc:
        print(f"[GetMusic] 解析失败: {exc}")
        return
    url = str((res or {}).get("music_url") or "")
    print(f"[GetMusic] query={query}, url={url}")
    if not url:
        return
    await _send_record(handler, event, url)


async def get_music(handler, event):
    clean_msg = handler.mp.get_clean_context(event)
    match = re.search(r"\[([^\]]*)\]", clean_msg)
    if not match:
        return
    query = match.group(1)
    media = getattr(handler, "media", None)
    if media is not None and hasattr(media, "send_music"):
        await media.send_music(event, query)
        return
    await _fallback_music(handler, event, query)


# 旧名字（BOTMain 的 action_registry 里就是 GetMusic）
GetMusic = get_music
