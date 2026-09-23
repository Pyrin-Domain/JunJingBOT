# -*- coding: utf-8 -*-
"""图片双哈希（aHash + dHash）——转发去重的图片二次校验

为什么需要
----------
纯图转发的文本指纹只能是
`old_forward|tsum9|群聊的聊天记录|浅奈: [图片]|浅奈: [图片]|...`：
"谁 + 几张图 + 总数"完全一样，于是「同一条记录被再次搬运」和
「另一个人发了张数相同的图」在文本上无法区分。这时把图片本身算成
128bit 指纹（aHash 抗缩放/亮度，dHash 抗轻微压缩与噪点），按汉明距离
比较即可判定"是不是同一份记录"。

设计约束
--------
* **只在文本指纹 weak（整段摘要只有 `昵称: [图片]`）时才调用**，
  普通文字转发一次也不下载图片，零开销；
* 网络、解码、依赖缺失一律安静失败（返回 None / 空列表），绝不影响转发主流程；
* 自带 LRU 缓存，同一个 url 只下载、解码一次。

哈希格式
--------
`f"{aHash:016x}{dHash:016x}"` = 32 位 hex = 128bit，高 64bit 是 aHash、
低 64bit 是 dHash，两张相同的图距离为 0，不同图片一般相差 60bit 以上。
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict

try:  # Pillow 缺失时整个图片校验优雅降级
    from PIL import Image, ImageOps

    IMAGE_HASH_AVAILABLE = True
except Exception:  # pragma: no cover - 环境没装 Pillow
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    IMAGE_HASH_AVAILABLE = False

try:
    import aiohttp
except Exception:  # pragma: no cover - 环境没装 aiohttp
    aiohttp = None  # type: ignore[assignment]

# aHash / dHash 都是 8x8 二值图 -> 64bit
_HASH_BITS = 64
A_HASH_HEX_LEN = 16
IMAGE_HASH_HEX_LEN = 32  # aHash 高 16 位 hex + dHash 低 16 位 hex

DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_COUNT = 5
DEFAULT_CONCURRENCY = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 超过 4MB 的图不下载（大概率不是表情/截图）

_CACHE_MAXSIZE = 512
_MISS = object()
# 缓存值就是“这张图的结果”：{"ok": bool, "hash": str, "reason": str} —— 连失败
# 原因一起缓存，否则第二次命中缓存就只能写个笼统的 cached_failure，
# 排查日志里“上次到底是 403 还是超时”就丢了。
_CACHE: "OrderedDict[str, dict]" = OrderedDict()


def _resample():
    """兼容新旧 Pillow：Image.Resampling.LANCZOS / Image.LANCZOS"""
    return getattr(Image, "Resampling", Image).LANCZOS


def _gray(source):
    """bytes / PIL.Image -> 灰度图（顺便按 EXIF 摆正方向）"""
    if isinstance(source, (bytes, bytearray, memoryview)):
        image = Image.open(io.BytesIO(bytes(source)))
    else:
        image = source
    return ImageOps.grayscale(ImageOps.exif_transpose(image))


def ahash64(source) -> int:
    """均值哈希：缩到 8x8 灰度，逐像素与均值比较。"""
    small = _gray(source).resize((8, 8), _resample())
    pixels = small.tobytes()
    average = sum(pixels) / len(pixels)
    result = 0
    for index, value in enumerate(pixels):
        if value > average:
            result |= 1 << index
    return result


def dhash64(source) -> int:
    """差值哈希：缩到 9x8 灰度，逐行比较左右相邻像素。"""
    small = _gray(source).resize((9, 8), _resample())
    pixels = small.tobytes()
    result = 0
    bit = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            if pixels[base + col] > pixels[base + col + 1]:
                result |= 1 << bit
            bit += 1
    return result


def image_hash(source) -> str:
    """bytes / PIL.Image -> 32 位 hex 的双哈希指纹；失败返回 ""。"""
    if not IMAGE_HASH_AVAILABLE:
        return ""
    try:
        return f"{ahash64(source):016x}{dhash64(source):016x}"
    except Exception:  # 坏图/非图片数据
        return ""


def hash_distance(a: str, b: str) -> int:
    """两个 32 位 hex 图片指纹的汉明距离（0 表示完全相同）。"""
    if not a or not b:
        return IMAGE_HASH_HEX_LEN * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_distance_64(a: int, b: int) -> int:
    """两个 64bit 子哈希的距离（调参/测试用）。"""
    return bin((a ^ b) & ((1 << _HASH_BITS) - 1)).count("1")


# ---------------------------------------------------------------- 下载 --------


def _cache_get(url: str):
    if url in _CACHE:
        _CACHE.move_to_end(url)
        return _CACHE[url]
    return _MISS


def _cache_put(url: str, value: dict) -> None:
    _CACHE[url] = value
    _CACHE.move_to_end(url)
    while len(_CACHE) > _CACHE_MAXSIZE:
        _CACHE.popitem(last=False)


def clear_cache() -> None:
    _CACHE.clear()


async def _download(session, url: str, timeout: float) -> tuple[bytes | None, str, dict]:
    """下载单张图，返回 (数据, 结果说明, 附加信息)。

    说明与附加信息都是给排查日志看的：`ok` 表示拿到数据，其余（http_403 /
    timeout / empty_body …）就是这张图没算出来的原因；附加信息里有状态码、
    Content-Type、字节数 —— 比如“下回来解不开”时，一眼能看出服务器给的
    其实是 text/html 的错误页（签名过期常见就是这个）。
    """
    info: dict = {}
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            info["status"] = int(response.status)
            ctype = str(response.headers.get("Content-Type") or "")[:80]
            if ctype:
                info["content_type"] = ctype
            if response.status != 200:
                return None, f"http_{response.status}", info
            # 必须循环读到 EOF：aiohttp 的 read(n) 只保证“等到有数据”，返回的是
            # 当前已到达的那一块（不等满 n 字节）。大图会被截断成半张，PIL 直接
            # 解不开 —— 线上实测每次刚好 16384 / 32768 字节，就是首包大小。
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    break
            data = b"".join(chunks)
            info["bytes"] = len(data)
            if not data:
                return None, "empty_body", info
            if len(data) > MAX_IMAGE_BYTES:
                return None, f"too_large>{MAX_IMAGE_BYTES}", info
            return data, "ok", info
    except asyncio.TimeoutError:
        return None, "timeout", info
    except Exception as exc:  # 网络/SSL/重定向等，记下原因供日志排查
        return None, f"{type(exc).__name__}: {exc}", info


def pick_urls(urls, max_count: int = DEFAULT_MAX_COUNT) -> list[str]:
    """去重 + 截断到 max_count 张（保持入参顺序）"""
    unique: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if text and text not in unique:
            unique.append(text)
        if max_count > 0 and len(unique) >= max_count:
            break
    return unique


async def hash_remote_images_report(
    urls: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    max_count: int = DEFAULT_MAX_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[str], dict]:
    """批量下载并计算图片双哈希，失败的图直接跳过（带逐张的诊断信息）。

    与 hash_remote_images 的区别只有“多返回一份报告”，报告直接进排查日志：
    这次打算下哪几张（`urls`）、每张是命中缓存还是现下（`items`）、
    成功几张（`hashed`），以及整体失败原因（`error`：no_url / aiohttp_missing /
    pillow_missing / all_failed …）。

    :return: (成功算出来的 32 位 hex 列表, 报告)
    """
    report: dict = {"urls": [], "url_cnt": 0, "items": [], "hashed": 0}
    if not IMAGE_HASH_AVAILABLE:
        report["error"] = "pillow_missing"
        return [], report
    picked = pick_urls(urls, max_count)
    report["urls"] = picked
    report["url_cnt"] = len(picked)
    if not picked or max_count <= 0:
        report["error"] = "no_url"
        return [], report
    results: dict[str, str | None] = {}
    items: dict[str, dict] = {}
    pending: list[str] = []
    for url in picked:
        cached = _cache_get(url)
        if cached is _MISS:
            pending.append(url)
        else:
            results[url] = cached.get("hash") or None
            items[url] = {**cached, "cache": True}  # 连同上次的失败原因一起回放
    if pending and aiohttp is None:
        report["error"] = "aiohttp_missing"
        for url in pending:
            results[url] = None
            items[url] = {"ok": False, "reason": "aiohttp_missing", "hash": ""}
        pending = []
    if pending:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def worker(session, url: str) -> None:
            async with semaphore:
                data, why, info = await _download(session, url, timeout)
            value = image_hash(data) if data else ""
            if data and not value:
                why = "decode_failed"
                # 带上开头几个字节：是 HTML 错误页还是一张坏图，一眼可见
                info["head"] = data[:120].decode("utf-8", "replace").replace("\n", " ")
            results[url] = value or None
            items[url] = {"ok": bool(value), "reason": why, "hash": value, **info}
            _cache_put(url, items[url])

        try:
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(
                    *(worker(session, url) for url in pending),
                    return_exceptions=True,
                )
        except Exception as exc:  # 网络层异常：安静降级，不影响转发
            report["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[图片双哈希] 下载图片失败，跳过图片校验: {exc}")
    report["items"] = [
        {"url": url, **items.get(url, {"ok": False, "reason": "not_tried", "hash": ""})}
        for url in picked
    ]
    hashes = [v for v in (results.get(u) for u in picked) if v]
    report["hashed"] = len(hashes)
    if not hashes and "error" not in report:
        report["error"] = "all_failed"
    return hashes, report


async def hash_remote_images(
    urls: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    max_count: int = DEFAULT_MAX_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[str]:
    """批量下载并计算图片双哈希，失败的图直接跳过。

    :return: 成功算出来的 32 位 hex 列表（去重，顺序与入参一致）
    """
    hashes, _report = await hash_remote_images_report(
        urls, timeout=timeout, max_count=max_count, concurrency=concurrency
    )
    return hashes


async def hash_remote_image(url: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """单张图（调试/测试用）"""
    hashes = await hash_remote_images(
        [url], timeout=timeout, max_count=1, concurrency=1
    )
    return hashes[0] if hashes else None
