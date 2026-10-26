"""图片 OCR：Windows 用系统 winrt，Linux 用 Tesseract。

搬运自旧 ``Minitor/extension.py``，缓存语义保持不变：

- key 是图片 URL，值只存识别出的 text；
- TTL 3600 秒，最多 128 条，超出按 LRU 淘汰；
- 下载失败 / 识别错误不写缓存；
- Windows 没有置信度接口，Linux ``conf <= 60`` 的词标记为 ``[?word]``。

返回结构始终是 ``{"text": ..., "raw": ...}``：命中缓存时 ``raw`` 为 ``None``，
实时识别时 Windows 返回 OCR lines、Linux 返回 pytesseract 的原始 dict。
"""

from __future__ import annotations

import asyncio
import platform
import time
from collections import OrderedDict

import aiohttp

DEBUG = False

# ── OCR 缓存配置 ─────────────────────────────
OCR_CACHE_MAXSIZE = 128
OCR_CACHE_TTL = 3600

# ── Windows 内置 OCR 依赖（仅在 Windows 上导入） ──
_WIN_OCR_AVAILABLE = False
if platform.system() == "Windows":
    try:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        _WIN_OCR_AVAILABLE = True
    except ImportError:
        if DEBUG:
            print("[OCR] winrt 未安装，Windows 内置 OCR 不可用")
        _WIN_OCR_AVAILABLE = False

# ── Linux Tesseract OCR 依赖（仅在非 Windows 上导入） ──
_LINUX_OCR_AVAILABLE = False
if platform.system() != "Windows":
    try:
        from io import BytesIO

        import pytesseract
        from PIL import Image

        _LINUX_OCR_AVAILABLE = True
    except ImportError:
        if DEBUG:
            print("[OCR] pytesseract/PIL 未安装，Linux OCR 不可用")
        _LINUX_OCR_AVAILABLE = False


class OCR:
    """跨平台 OCR。

    - Windows → Windows.Media.Ocr（系统内置，无置信度）
    - 其它    → pytesseract（chi_sim+eng，低置信度词加 ``[?...]``）
    """

    _OCR_CONF_THRESHOLD = 60

    def __init__(self) -> None:
        self._platform = platform.system()
        # url -> (timestamp, text)
        self._ocr_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        if not _WIN_OCR_AVAILABLE and not _LINUX_OCR_AVAILABLE:
            print(
                f"[OCR] 警告: 当前平台 {self._platform} 无可用 OCR 后端，"
                "napcat_ocr() 将始终返回错误信息"
            )
        if DEBUG:
            print(f"[OCR] OCR 初始化完成，平台: {self._platform}")

    # ── 公开方法 ──────────────────────────────
    async def recognize(self, img_url: str, re_ocr: bool = False) -> dict:
        """异步 OCR：带 TTL + LRU 缓存。"""
        if not re_ocr:
            cached = self._get_cache(img_url)
            if cached is not None:
                if DEBUG:
                    print(f"[OCR] 缓存命中: {img_url[:60]}...")
                return {"text": cached, "raw": None}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    img_url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return {
                            "text": f"[OCR 下载失败: HTTP {resp.status}]",
                            "raw": None,
                        }
                    img_bytes = await resp.read()
        except Exception as exc:
            return {"text": f"[OCR 下载失败: {exc}]", "raw": None}

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._do_ocr, img_bytes)

        # 只缓存成功的 text；错误信息以 ``[OCR`` 开头，不进入缓存。
        text = str(result.get("text") or "")
        if not text.startswith("[OCR"):
            self._set_cache(img_url, text)

        return result

    # 旧入口别名，便于迁移期直接替换 Extension.napcat_ocr。
    async def napcat_ocr(self, img_url: str, re_ocr: bool = False) -> dict:
        return await self.recognize(img_url, re_ocr=re_ocr)

    # ── 缓存池：TTL 过期 + LRU 淘汰 ───────────
    def _get_cache(self, url: str) -> str | None:
        entry = self._ocr_cache.get(url)
        if entry is None:
            return None
        ts, text = entry
        if time.time() - ts > OCR_CACHE_TTL:
            del self._ocr_cache[url]
            return None
        self._ocr_cache.move_to_end(url)
        return text

    def _set_cache(self, url: str, text: str) -> None:
        now = time.time()

        # 惰性清理过期条目（只扫前 32 条，与旧版一致）
        expired_keys = []
        for index, (key, (ts, _)) in enumerate(self._ocr_cache.items()):
            if index >= 32:
                break
            if now - ts > OCR_CACHE_TTL:
                expired_keys.append(key)
        for key in expired_keys:
            del self._ocr_cache[key]

        while len(self._ocr_cache) >= OCR_CACHE_MAXSIZE:
            lru_key, _ = self._ocr_cache.popitem(last=False)
            if DEBUG:
                print(f"[OCR] LRU 淘汰: {str(lru_key)[:60]}...")

        self._ocr_cache[url] = (now, text)
        self._ocr_cache.move_to_end(url)

    # ── 内部实现 ──────────────────────────────
    def _do_ocr(self, img_bytes: bytes) -> dict:
        if self._platform == "Windows":
            return self._ocr_windows(img_bytes)
        return self._ocr_linux(img_bytes)

    def _ocr_windows(self, img_bytes: bytes) -> dict:
        """Windows 内置 OCR：bytes → BitmapDecoder → OCR（线程安全）。"""
        if not _WIN_OCR_AVAILABLE:
            return {"text": "[OCR Windows 后端不可用]", "raw": None}
        try:
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return {"text": "[OCR 引擎不可用，请检查系统语言包]", "raw": None}
            bitmap = self._bytes_to_bitmap(img_bytes)
            ocr_result = engine.recognize_async(bitmap).get()
            text_list = [line.text for line in ocr_result.lines]
            return {"text": "\n".join(text_list), "raw": ocr_result.lines}
        except Exception as exc:
            return {"text": f"[OCR 错误: {exc}]", "raw": None}

    @staticmethod
    def _bytes_to_bitmap(img_bytes: bytes):
        """bytes → InMemoryRandomAccessStream → BitmapDecoder → SoftwareBitmap。"""
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(img_bytes)
        writer.store_async().get()
        writer.detach_stream()
        stream.seek(0)
        decoder = BitmapDecoder.create_async(stream).get()
        return decoder.get_software_bitmap_async().get()

    def _ocr_linux(self, img_bytes: bytes) -> dict:
        """Linux Tesseract OCR：bytes → PIL Image → pytesseract。"""
        if not _LINUX_OCR_AVAILABLE:
            return {"text": "[OCR Linux 后端不可用]", "raw": None}
        try:
            image = Image.open(BytesIO(img_bytes))
            raw_result = pytesseract.image_to_data(
                image, lang="chi_sim+eng", output_type=pytesseract.Output.DICT
            )

            prev_line_key = None
            cur_line_words: list[str] = []
            text_lines: list[str] = []

            for index in range(len(raw_result["level"])):
                if raw_result["level"][index] != 5:  # 只处理词级别
                    continue
                word = (raw_result["text"][index] or "").strip()
                if not word:
                    continue

                line_key = (
                    raw_result["block_num"][index],
                    raw_result["line_num"][index],
                )
                if prev_line_key is not None and line_key != prev_line_key:
                    text_lines.append(" ".join(cur_line_words))
                    cur_line_words = []
                prev_line_key = line_key

                conf = raw_result["conf"][index]
                if conf > self._OCR_CONF_THRESHOLD:
                    cur_line_words.append(word)
                else:
                    cur_line_words.append(f"[?{word}]")

            if cur_line_words:
                text_lines.append(" ".join(cur_line_words))

            return {"text": "\n".join(text_lines), "raw": raw_result}
        except Exception as exc:
            return {"text": f"[OCR 识别错误: {exc}]", "raw": None}


# 旧代码里类名是 Extension；保留别名，迁移期不用改所有调用点。
Extension = OCR

_default_ocr: OCR | None = None


def get_default_ocr() -> OCR:
    global _default_ocr
    if _default_ocr is None:
        _default_ocr = OCR()
    return _default_ocr


async def napcat_ocr(img_url: str, re_ocr: bool = False) -> dict:
    """模块级便捷入口，使用共享的 OCR 实例与缓存。"""
    return await get_default_ocr().recognize(img_url, re_ocr=re_ocr)


__all__ = [
    "OCR",
    "Extension",
    "OCR_CACHE_MAXSIZE",
    "OCR_CACHE_TTL",
    "get_default_ocr",
    "napcat_ocr",
]
