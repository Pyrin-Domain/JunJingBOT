"""点歌下载源。

搬运自旧 ``MusicServer/main.py``：搜索页 → 第一首结果的播放器页 → 远程
``music_url``。只解析地址，不下载音频；仍然走下载池，是为了统一超时、
重试和统计。

保持旧版 HTTP 头与 20 秒超时不变。
"""

from __future__ import annotations

import asyncio
import base64
import re
import urllib.parse
import urllib.request
from typing import Any

from ..storage import DownloadStorage
from ..task import DownloadResult, DownloadTask
from .base import BaseProvider

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
FETCH_TIMEOUT = 20


def fetch_url(url: str) -> str:
    """拉取 HTML（同步，供线程池调用）。"""
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read().decode("utf-8", "ignore")


def _extract_first_result(
    html: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    match = re.search(
        r'<div class="result-item"[^>]*data-rid="(?P<rid>\d+)".*?'
        r'<div class="result-title">(?P<title>.*?)</div>.*?'
        r'<div class="result-artist">(?P<artist>.*?)</div>.*?'
        r'<div class="result-album">(?P<album>.*?)</div>',
        html,
        re.S,
    )
    if not match:
        return None, None, None, None

    rid = match.group("rid")
    title = re.sub(r"<.*?>", "", match.group("title")).strip()
    artist = re.sub(r"<.*?>", "", match.group("artist")).strip()
    album = re.sub(r"<.*?>", "", match.group("album")).strip()
    album = album.replace("专辑:", "").strip()
    return rid, title, artist, album


def _extract_player_music_url(html: str) -> str | None:
    pattern = re.search(r"let code\s*=\s*['\"](?P<code>[A-Za-z0-9+/=]+)['\"]", html, re.S)
    if not pattern:
        return None

    encoded = pattern.group("code")
    try:
        decoded = base64.b64decode(encoded).decode("utf-8", "ignore")
        return decoded.strip()
    except Exception:
        return None


def search_and_resolve_first_song(keyword: str) -> dict[str, Any]:
    """搜索关键字并解析第一首歌曲的远程播放地址。"""
    search_url = f"https://higequ.com/s/{urllib.parse.quote(keyword)}/"
    search_html = fetch_url(search_url)
    rid, title, artist, album = _extract_first_result(search_html)
    if not rid:
        raise ValueError(f"没有在 {search_url} 找到结果")

    player_url = f"https://higequ.com/player/{rid}/"
    player_html = fetch_url(player_url)
    music_url = _extract_player_music_url(player_html)
    if not music_url:
        music_url = ""

    return {
        "keyword": keyword,
        "title": title,
        "artist": artist,
        "album": album,
        "rid": rid,
        "player_url": player_url,
        "music_url": music_url,
    }


class MusicProvider(BaseProvider):
    kind = "music"

    @staticmethod
    def search_and_resolve_first_song(keyword: str) -> dict[str, Any]:
        """兼容旧入口：搜索页 → 播放器页 → 远程 ``music_url``。"""
        return search_and_resolve_first_song(keyword)

    async def download(
        self, task: DownloadTask, storage: DownloadStorage
    ) -> DownloadResult:
        keyword = str(task.source or "").strip()
        if not keyword:
            raise ValueError("点歌关键字不能为空")

        info = await asyncio.to_thread(search_and_resolve_first_song, keyword)
        music_url = str(info.get("music_url") or "")
        meta = dict(info)
        # 业务层使用的新字段名；旧字段（title / artist）同时保留。
        meta["name"] = info.get("title") or ""
        meta["singer"] = info.get("artist") or ""
        meta["music_url"] = music_url
        return DownloadResult(
            path=None,
            size=0,
            mime="audio/mpeg",
            url=music_url,
            meta=meta,
        )


__all__ = [
    "DEFAULT_HEADERS",
    "MusicProvider",
    "fetch_url",
    "search_and_resolve_first_song",
]
