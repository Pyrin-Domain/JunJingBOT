# -*- coding: utf-8 -*-
"""图片双哈希的下载上限。

事故背景：`MAX_IMAGE_BYTES` 原来卡 4MB，一张 4.2MB 的截图（转发的单图聊天记录）
直接被判 ``too_large`` → 双哈希一个都算不出来 → weak 指纹只剩「谁 + [图片]」→
同一张图 71 秒内被搬了两次。上限放到 16MB，并给大 JPEG 加 draft 缩放解码。
"""
import asyncio
import io

import media.image_hash as ih


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, size):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _Resp:
    def __init__(self, chunks):
        self.status = 200
        self.headers = {"Content-Type": "image/png"}
        self.content = _Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, chunks):
        self._chunks = chunks

    def get(self, url, timeout=None):
        return _Resp(self._chunks)


def _chunks(total: int, step: int = 64 * 1024):
    left = total
    out = []
    while left > 0:
        size = min(step, left)
        out.append(b"x" * size)
        left -= size
    return out


def test_default_cap_accepts_ordinary_screenshots():
    """4MB 以上是常态（手机截图 / 原图），上限不能再卡在 4MB。"""
    assert ih.MAX_IMAGE_BYTES >= 8 * 1024 * 1024
    assert ih.MAX_IMAGE_BYTES >= ih.DRAFT_DECODE_BYTES


def test_download_keeps_images_between_old_cap_and_new_cap():
    data = _chunks(5 * 1024 * 1024)  # 曾经会被 too_large 挡掉的尺寸
    got, why, info = asyncio.run(ih._download(_Session(data), "http://x", 1.0))
    assert why == "ok"
    assert got is not None and len(got) == 5 * 1024 * 1024
    assert info["bytes"] == 5 * 1024 * 1024


def test_download_still_rejects_anything_above_the_cap():
    data = _chunks(2 * 1024 * 1024)
    got, why, info = asyncio.run(
        ih._download(_Session(data), "http://x", 1.0, max_bytes=1024 * 1024)
    )
    assert got is None
    assert why == f"too_large>{1024 * 1024}"
    # 超限就停：info["bytes"] 是「读到这里为止」，不是整张图的真实大小
    assert info["bytes"] > 1024 * 1024


def test_draft_decode_does_not_change_the_hash(monkeypatch):
    """大 JPEG 走 draft 缩放解码，同一张图两条路径必须算出同一个指纹。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 30, 200)).save(buf, format="JPEG")
    raw = buf.getvalue()

    monkeypatch.setattr(ih, "DRAFT_DECODE_BYTES", len(raw))
    with_draft = ih.image_hash(raw)
    monkeypatch.setattr(ih, "DRAFT_DECODE_BYTES", 10 ** 9)
    without = ih.image_hash(raw)

    assert len(with_draft) == ih.IMAGE_HASH_HEX_LEN
    assert with_draft == without
