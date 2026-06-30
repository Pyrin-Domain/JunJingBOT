import aiohttp
import asyncio
import platform
import sys
import time
from collections import OrderedDict

DEBUG = True

# ── OCR 缓存配置 ─────────────────────────────
OCR_CACHE_MAXSIZE = 128    # 最多缓存条数
OCR_CACHE_TTL = 3600       # 单条过期时间（秒），1小时


# ──────────────────────────────────────────────
# Windows 内置 OCR 依赖（仅在 Windows 上导入）
# ──────────────────────────────────────────────
_WIN_OCR_AVAILABLE = False
if platform.system() == "Windows":
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import (
            InMemoryRandomAccessStream,
            DataWriter,
        )

        _WIN_OCR_AVAILABLE = True
    except ImportError:
        if DEBUG:
            print("[OCR] winrt 未安装，Windows 内置 OCR 不可用")
        _WIN_OCR_AVAILABLE = False

# ──────────────────────────────────────────────
# Linux Tesseract OCR 依赖（仅在非 Windows 上导入）
# ──────────────────────────────────────────────
_LINUX_OCR_AVAILABLE = False
if platform.system() != "Windows":
    try:
        from io import BytesIO
        from PIL import Image
        import pytesseract

        _LINUX_OCR_AVAILABLE = True
    except ImportError:
        if DEBUG:
            print("[OCR] pytesseract/PIL 未安装，Linux OCR 不可用")
        _LINUX_OCR_AVAILABLE = False


# ══════════════════════════════════════════════
# 统一 Extension 类
# ══════════════════════════════════════════════
class Extension:
    """
    跨平台 OCR 扩展

    - Windows → 使用 winrt Windows.Media.Ocr（内置，无需额外安装）
    - Linux   → 使用 pytesseract（需安装 tesseract-ocr 及语言包）
    - 自动检测平台，屏蔽底层差异
    """

    def __init__(self):
        self._platform = platform.system()
        # OCR 缓存池: OrderedDict[img_url, (timestamp, text)]
        # OrderedDict 保证插入顺序，用于 LRU 淘汰
        self._ocr_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        if not _WIN_OCR_AVAILABLE and not _LINUX_OCR_AVAILABLE:
            print(
                f"[OCR] 警告: 当前平台 {self._platform} 无可用 OCR 后端，"
                "napcat_ocr() 将始终返回错误信息"
            )
        if DEBUG:
            print(f"[OCR] Extension 初始化完成，平台: {self._platform}")

    # ── 公开方法 ──────────────────────────────

    async def napcat_ocr(self, img_url: str, re_ocr: bool = False) -> dict:
        """
        异步 OCR：带缓存池，自动 TTL 过期 + LRU 淘汰

        参数:
            img_url: QQ 图片下载 URL
            re_ocr:  是否强制重新 OCR（跳过缓存，默认 False）

        返回:
            {"text": "识别的文本", "raw": None}
            （缓存中只存 text，raw 仅在实时识别时返回）
        """
        # ── 非强制刷新 → 查缓存 ──
        if not re_ocr:
            cached = self._get_cache(img_url)
            if cached is not None:
                DEBUG and print(f"[OCR] 缓存命中: {img_url[:60]}...")
                return {"text": cached, "raw": None}

        # ── 缓存未命中 / 强制刷新 → 下载 + OCR ──
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
        except Exception as e:
            return {"text": f"[OCR 下载失败: {e}]", "raw": None}

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._do_ocr, img_bytes)

        # ── 仅缓存 text，不缓存 raw ──
        if not result["text"].startswith("[OCR"):
            self._set_cache(img_url, result["text"])

        return result

    # ── 缓存池：TTL 过期 + LRU 淘汰 ───────────

    def _get_cache(self, url: str) -> str | None:
        """查询缓存，命中时刷新 LRU 顺序"""
        entry = self._ocr_cache.get(url)
        if entry is None:
            return None
        ts, text = entry
        # TTL 过期检查
        if time.time() - ts > OCR_CACHE_TTL:
            del self._ocr_cache[url]
            return None
        # 命中 → 移到末尾（LRU 保活）
        self._ocr_cache.move_to_end(url)
        return text

    def _set_cache(self, url: str, text: str):
        """写入缓存，插入前淘汰过期 / 超量条目"""
        now = time.time()

        # 1. 惰性清理过期条目（只扫前 32 条，避免全表扫描）
        expired_keys = []
        for _i, (k, (ts, _)) in enumerate(self._ocr_cache.items()):
            if _i >= 32:
                break
            if now - ts > OCR_CACHE_TTL:
                expired_keys.append(k)
        for k in expired_keys:
            del self._ocr_cache[k]

        # 2. 如果仍然超过上限 → LRU 淘汰（从头部逐出）
        while len(self._ocr_cache) >= OCR_CACHE_MAXSIZE:
            lru_key, _ = self._ocr_cache.popitem(last=False)
            DEBUG and print(f"[OCR] LRU 淘汰: {lru_key[:60]}...")

        # 3. 写入（如果已存在则更新）
        self._ocr_cache[url] = (now, text)
        self._ocr_cache.move_to_end(url)

    # ── 内部实现 ──────────────────────────────

    def _do_ocr(self, img_bytes: bytes) -> dict:
        """根据当前平台选择 OCR 引擎（在线程池中执行）"""
        if self._platform == "Windows":
            return self._ocr_windows(img_bytes)
        else:
            return self._ocr_linux(img_bytes)

    # ── Windows OCR（winrt）────────────────────
    # Windows OCR 不暴露置信度接口，所有文本直接保留
    # 返回时 lines 已按阅读顺序（从上到下、从左到右）排列
    # 每个 OcrLine → .Text + .Words[].BoundingRect (像素坐标)

    def _ocr_windows(self, img_bytes: bytes) -> dict:
        """Windows 内置 OCR：bytes → BitmapDecoder → OCR（线程安全）"""
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
        except Exception as e:
            return {"text": f"[OCR 错误: {e}]", "raw": None}

    @staticmethod
    def _bytes_to_bitmap(img_bytes: bytes):
        """bytes → InMemoryRandomAccessStream → BitmapDecoder → SoftwareBitmap"""
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(img_bytes)
        writer.store_async().get()
        writer.detach_stream()
        stream.seek(0)
        decoder = BitmapDecoder.create_async(stream).get()
        return decoder.get_software_bitmap_async().get()

    # ── Linux OCR（Tesseract）──────────────────
    # 每个词有置信度 conf[0~100]，像素坐标 left/top/width/height
    # 结果按阅读顺序排列（block_num → line_num → word_num）
    # 仅保留词级别（level==5），避免多层级重复文本

    _OCR_CONF_THRESHOLD = 60  # 置信度阈值，低于此值标记为 [?原文]

    def _ocr_linux(self, img_bytes: bytes) -> dict:
        """Linux Tesseract OCR：bytes → PIL Image → pytesseract"""
        if not _LINUX_OCR_AVAILABLE:
            return {"text": "[OCR Linux 后端不可用]", "raw": None}
        try:
            img = Image.open(BytesIO(img_bytes))
            lang_config = "chi_sim+eng"
            raw_result = pytesseract.image_to_data(
                img, lang=lang_config, output_type=pytesseract.Output.DICT
            )

            # 按 Tesseract 返回顺序（已是阅读顺序）逐词处理
            # 用 (block_num, line_num) 标识行，自动拼回行结构
            prev_line_key = None
            cur_line_words: list[str] = []
            text_lines: list[str] = []

            for i in range(len(raw_result["level"])):
                if raw_result["level"][i] != 5:  # 只处理词级别
                    continue
                word = (raw_result["text"][i] or "").strip()
                if not word:
                    continue

                line_key = (raw_result["block_num"][i], raw_result["line_num"][i])
                if prev_line_key is not None and line_key != prev_line_key:
                    text_lines.append(" ".join(cur_line_words))
                    cur_line_words = []
                prev_line_key = line_key

                conf = raw_result["conf"][i]
                if conf > self._OCR_CONF_THRESHOLD:
                    cur_line_words.append(word)
                else:
                    cur_line_words.append(f"[?{word}]")

            if cur_line_words:
                text_lines.append(" ".join(cur_line_words))

            full_text = "\n".join(text_lines)
            return {"text": full_text, "raw": raw_result}
        except Exception as e:
            return {"text": f"[OCR 识别错误: {str(e)}]", "raw": None}
