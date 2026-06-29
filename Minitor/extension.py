import aiohttp
import asyncio
import threading

from winrt.windows.media.ocr import OcrEngine
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

DEBUG = True


def _bytes_to_bitmap(img_bytes: bytes):
    """bytes → InMemoryRandomAccessStream → BitmapDecoder → SoftwareBitmap（纯内存）"""
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(img_bytes)
    writer.store_async().get()
    writer.detach_stream()
    stream.seek(0)
    decoder = BitmapDecoder.create_async(stream).get()
    return decoder.get_software_bitmap_async().get()


class Extension:
    """Windows 内置 OCR 扩展，后台线程初始化引擎"""

    def __init__(self):
        self._ocr_engine = None
        self._ocr_ready = threading.Event()
        self._init_thread = threading.Thread(target=self._init_ocr_bg, daemon=True)
        self._init_thread.start()

    def _init_ocr_bg(self):
        """后台线程：加载 Windows OCR 引擎（毫秒级，极快）"""
        DEBUG and print("[OCR] 后台初始化 Windows OCR 引擎 ...")
        try:
            self._ocr_engine = OcrEngine.try_create_from_user_profile_languages()
            self._ocr_ready.set()
            DEBUG and print("[OCR] Windows OCR 引擎就绪")
        except Exception as e:
            print(f"[OCR] 初始化失败: {e}")
            self._ocr_ready.set()

    @property
    def ocr_engine(self):
        """阻塞等待 OCR 引擎就绪"""
        self._ocr_ready.wait()
        return self._ocr_engine

    async def napcat_ocr(self, img_url: str) -> dict:
        """
        异步 OCR：下载 QQ 图片 → Windows 内置 OCR 识别 → 返回文本
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return {"text": f"[OCR 下载失败: HTTP {resp.status}]", "raw": None}
                    img_bytes = await resp.read()
        except Exception as e:
            return {"text": f"[OCR 下载失败: {e}]", "raw": None}

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._do_ocr, img_bytes)

    def _do_ocr(self, img_bytes: bytes) -> dict:
        """纯内存 OCR：bytes → 流 → BitmapDecoder → OCR（线程池中执行）"""
        try:
            # 每个 executor 线程重新获取引擎（WinRT COM 有线程亲和性）
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return {"text": "[OCR 引擎不可用]", "raw": None}
            bitmap = _bytes_to_bitmap(img_bytes)
            ocr_result = engine.recognize_async(bitmap).get()
            text_list = [line.text for line in ocr_result.lines]
            return {"text": "\n".join(text_list), "raw": ocr_result.lines}
        except Exception as e:
            return {"text": f"[OCR 错误: {e}]", "raw": None}
