"""构造 NapCat 的图片消息段。"""

from __future__ import annotations

from .models import ImageRef


def image_segment(ref: ImageRef) -> dict:
    """与旧版 ``send_group_img`` 完全一致的段结构（summary 始终存在）。"""
    return {
        "type": "image",
        "data": {"file": ref.napcat_path, "summary": ref.summary},
    }


def image_message(ref: ImageRef) -> list[dict]:
    return [image_segment(ref)]


__all__ = ["image_segment", "image_message"]
