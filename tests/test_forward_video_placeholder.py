# -*- coding: utf-8 -*-
"""视频占位符必须和图片分开。"""
from __future__ import annotations

from bot.services.dedup import (
    build_fingerprint,
    cq_to_placeholder,
    forward_content_items,
    normalize_body,
    segments_placeholder,
)


def test_cq_video_placeholder_is_not_image():
    assert cq_to_placeholder("[CQ:video,file=x.mp4]") == "[视频]"
    assert segments_placeholder([{"type": "video", "data": {"file": "x.mp4"}}]) == "[视频]"


def test_normalize_body_keeps_video_separate():
    assert normalize_body("[视频][图片][视频] 你好") == "[视频][图片][视频] 你好"
    # 图片仍按老规矩合并，视频不被吞掉
    assert normalize_body("[图片][图片][视频][图片]") == "[图片][视频][图片]"


def test_forward_content_items_counts_video_as_media():
    messages = [
        {
            "sender": {"card": "A"},
            "message": [
                {"type": "video", "data": {"file": "x.mp4"}},
                {"type": "image", "data": {"file": "y.jpg"}},
            ],
        }
    ]
    items, total, media = forward_content_items(messages)
    assert media == 2
    fingerprint = build_fingerprint(
        {"message_id": 1}, "old_forward", forward_messages=messages
    )
    assert "[视频]" in fingerprint["text"]
    assert "[图片]" in fingerprint["text"]
    assert fingerprint["weak"] is True


# ── 同一份内容在两个后端必须拼出同一个占位文本 ──────────────────
def test_sticker_placeholder_is_backend_independent():
    """表情在 SnowLuma 是 mface，在 NapCat 是带 emoji_id 的 image。

    指纹只按**内容**算（不含发送者），所以这两种形状必须塌成同一个占位，
    否则同一条转发换个后端再算一次就会 miss。
    """
    snowluma_sticker = {"type": "mface", "data": {"summary": "[嘬嘬]", "emoji_id": "x"}}
    napcat_sticker = {
        "type": "image",
        "data": {"summary": "[唤马]", "emoji_id": "d22af9", "key": "k"},
    }
    napcat_photo = {"type": "image", "data": {"summary": "[图片]", "sub_type": 0}}

    # 表情和普通图片在指纹里一律是「图片」：媒体只留占位，不区分种类
    assert segments_placeholder([snowluma_sticker]) == "[图片]"
    assert segments_placeholder([napcat_sticker]) == "[图片]"
    assert segments_placeholder([napcat_photo]) == "[图片]"


def test_photo_and_sticker_produce_same_fingerprint_body():
    """同一个「一张图」的内容，两种后端算出来的正文（进入指纹的那部分）一致。"""
    from bot.services.dedup import normalize_body

    snowluma = segments_placeholder([{"type": "mface", "data": {"summary": "[动画表情]"}}])
    napcat = segments_placeholder(
        [{"type": "image", "data": {"summary": "[唤马]", "emoji_id": "x", "key": "k"}}]
    )

    assert normalize_body(snowluma) == normalize_body(napcat) == "[图片]"
