import base64
import re
import urllib.parse
import urllib.request
from typing import Optional


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "ignore")


def _extract_first_result(html: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    match = re.search(r'<div class="result-item"[^>]*data-rid="(?P<rid>\d+)".*?<div class="result-title">(?P<title>.*?)</div>.*?<div class="result-artist">(?P<artist>.*?)</div>.*?<div class="result-album">(?P<album>.*?)</div>', html, re.S)
    if not match:
        return None, None, None, None

    rid = match.group("rid")
    title = re.sub(r"<.*?>", "", match.group("title")).strip()
    artist = re.sub(r"<.*?>", "", match.group("artist")).strip()
    album = re.sub(r"<.*?>", "", match.group("album")).strip()
    album = album.replace("专辑:", "").strip()
    return rid, title, artist, album


def _extract_player_music_url(html: str) -> Optional[str]:
    pattern = re.search(r"let code\s*=\s*['\"](?P<code>[A-Za-z0-9+/=]+)['\"]", html, re.S)
    if not pattern:
        return None

    encoded = pattern.group("code")
    try:
        decoded = base64.b64decode(encoded).decode("utf-8", "ignore")
        return decoded.strip()
    except Exception:
        return None


def search_and_resolve_first_song(keyword: str) -> dict:
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


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]).strip() or "夜空中最亮的星"
    result = search_and_resolve_first_song(query)
    print(result)
