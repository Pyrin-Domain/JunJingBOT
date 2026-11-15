"""媒体能力：OCR、图片哈希、B 站 / 点歌发送粘合层。

业务代码只需要：

    from media import MediaService, OCR, napcat_ocr, image_hash

    service = MediaService(api, images, downloads, settings)
    await service.send_bili_video(event, url)

图片哈希的公开函数与旧 ``Minitor/image_hash.py`` 同名同义。
"""

from __future__ import annotations

from . import image_hash as image_hash
from . import video_hash as video_hash
from .bili_history import DEFAULT_KEEP, BiliHistory, video_key
from .image_hash import (
    A_HASH_HEX_LEN,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_COUNT,
    DEFAULT_TIMEOUT,
    IMAGE_HASH_AVAILABLE,
    IMAGE_HASH_HEX_LEN,
    MAX_IMAGE_BYTES,
    ahash64,
    clear_cache,
    dhash64,
    hash_distance,
    hash_distance_64,
    hash_remote_image,
    hash_remote_images,
    hash_remote_images_report,
    pick_urls,
)
from .ocr import OCR, get_default_ocr, napcat_ocr
from .service import DEFAULT_SEND_TIMEOUT, MediaService
from .video_hash import (
    clean_video_signatures,
    hash_remote_videos,
    hash_remote_videos_report,
    video_signature,
)

# 兼容想直接拿到指纹函数的调用方；``image_hash`` 这个名字保留给子模块。
image_fingerprint = image_hash.image_hash

__all__ = [
    "MediaService",
    "DEFAULT_SEND_TIMEOUT",
    "OCR",
    "get_default_ocr",
    "napcat_ocr",
    # B 站下载历史
    "BiliHistory",
    "video_key",
    "DEFAULT_KEEP",
    # 图片哈希（``image_hash`` 是模块，函数改名导出为 image_fingerprint）
    "image_hash",
    "image_fingerprint",
    "IMAGE_HASH_AVAILABLE",
    "A_HASH_HEX_LEN",
    "IMAGE_HASH_HEX_LEN",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_COUNT",
    "DEFAULT_CONCURRENCY",
    "MAX_IMAGE_BYTES",
    "ahash64",
    "dhash64",
    "hash_distance",
    "hash_distance_64",
    "pick_urls",
    "clear_cache",
    "hash_remote_images_report",
    "hash_remote_images",
    "hash_remote_image",
    # 视频指纹（封面帧 + 第10帧 + 可选字节哈希）
    "video_hash",
    "video_signature",
    "hash_remote_videos_report",
    "hash_remote_videos",
    "clean_video_signatures",
]

