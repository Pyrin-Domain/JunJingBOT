"""``AbstractionLayer.segments`` 的段语义归一。

样本全部来自 2026-10-31 的实测：SnowLuma（:3210）261 个段、NapCat（:3000）
50 个段，逐条统计出来的真实形状。这里把它们固化成用例，避免以后换协议端
又要重新踩一遍「表情被当图片搬走」。
"""

from __future__ import annotations

import pytest

from AbstractionLayer.segments import (
    OTHER,
    PHOTO,
    STICKER,
    file_name_from_source,
    has_photo,
    image_kind,
    is_photo,
    iter_photos,
    normalize_outgoing,
    photo_urls,
    sticker_name,
)

# ── 真实样本 ────────────────────────────────────────────────────

SNOWLUMA_PHOTO = {
    "type": "image",
    "data": {"file": "ABC.jpg", "url": "https://x/y", "sub_type": 0, "summary": "[图片]"},
}
SNOWLUMA_CUSTOM_FACE = {
    "type": "image",
    "data": {"file": "DEF.jpg", "url": "https://x/y", "sub_type": 1, "summary": "[动画表情]"},
}
SNOWLUMA_MARKET_FACE_7 = {
    "type": "image",
    "data": {"file": "GHI.jpg", "url": "https://x/y", "sub_type": 7, "summary": "[呜呜]"},
}
# NapCat 商城表情：sub_type 字段**完全不存在**
NAPCAT_MARKET_FACE = {
    "type": "image",
    "data": {
        "file": "d2-d22af900.gif",
        "url": "https://gxh.vip.qq.com/club/item/parcel/item/d2/d22af900/raw300.gif",
        "summary": "[唤马]",
        "key": "fdcbb9d0e86eb2ba",
        "emoji_id": "d22af90044ccbac49c19a13ec5365928",
        "emoji_package_id": "1",
    },
}
NAPCAT_PHOTO = {
    "type": "image",
    "data": {"file": "JKL.jpg", "url": "https://x/y", "sub_type": 0, "summary": "[图片]", "file_size": 10240},
}


def _msg(*segments) -> dict:
    return {"message": list(segments)}


# ── 分类 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "segment, expected",
    [
        (SNOWLUMA_PHOTO, PHOTO),
        (NAPCAT_PHOTO, PHOTO),
        (SNOWLUMA_CUSTOM_FACE, STICKER),
        (SNOWLUMA_MARKET_FACE_7, STICKER),
        (NAPCAT_MARKET_FACE, STICKER),
        ({"type": "mface", "data": {"summary": "[嘬嘬]"}}, STICKER),
        ({"type": "text", "data": {"text": "hi"}}, OTHER),
        ({"type": "video", "data": {"file": "a.mp4"}}, OTHER),
        ("not-a-dict", OTHER),
        (None, OTHER),
    ],
)
def test_image_kind(segment, expected):
    assert image_kind(segment) == expected


def test_is_photo_only_for_real_images():
    assert is_photo(SNOWLUMA_PHOTO) is True
    assert is_photo(NAPCAT_MARKET_FACE) is False


# ── 太松的反例：表情不能被算成图片 ──────────────────────────────


@pytest.mark.parametrize(
    "segment",
    [SNOWLUMA_CUSTOM_FACE, SNOWLUMA_MARKET_FACE_7, NAPCAT_MARKET_FACE],
)
def test_stickers_are_not_photos(segment):
    assert has_photo(_msg(segment)) is False


def test_sticker_without_sub_type_is_caught_by_emoji_fields():
    # NapCat 真实样本：sub_type 缺失，summary 是「[唤马]」，靠 emoji_id 认
    assert has_photo(_msg(NAPCAT_MARKET_FACE)) is False
    # 就算 summary 被清空，emoji_id 还在也要认出来
    blank = {"type": "image", "data": {"file": "x.gif", "summary": "", "emoji_id": "abc"}}
    assert has_photo(_msg(blank)) is False


def test_sticker_without_sub_type_is_caught_by_summary():
    # 兜底：既没 sub_type 也没 emoji_id，但 summary 是表情名
    bare = {"type": "image", "data": {"file": "x.gif", "summary": "[咕呜]"}}
    assert has_photo(_msg(bare)) is False


# ── 太严的反例：真图片不能被静默丢掉 ────────────────────────────


def test_photo_with_empty_summary_counts():
    # 旧判据「summary 为空才算图片」在两端都恒为假，这里保证不会再走回头路
    assert has_photo(_msg({"type": "image", "data": {"file": "a.jpg", "summary": ""}})) is True
    assert has_photo(_msg({"type": "image", "data": {"file": "a.jpg"}})) is True


def test_photo_with_filename_summary_counts():
    seg = {"type": "image", "data": {"file": "a.jpg", "summary": "IMG_0042.jpg"}}
    assert has_photo(_msg(seg)) is True


def test_sub_type_as_string_is_handled():
    assert has_photo(_msg({"type": "image", "data": {"summary": "[图片]", "sub_type": "0"}})) is True
    assert has_photo(_msg({"type": "image", "data": {"summary": "", "sub_type": "1"}})) is False
    # 脏值按 0 处理，不因为类型不对就判成表情
    assert has_photo(_msg({"type": "image", "data": {"summary": "", "sub_type": "abc"}})) is True


# ── 组合 ────────────────────────────────────────────────────────


def test_sticker_before_photo_still_finds_photo():
    assert has_photo(_msg(SNOWLUMA_CUSTOM_FACE, SNOWLUMA_PHOTO)) is True
    assert has_photo(_msg(NAPCAT_MARKET_FACE, NAPCAT_PHOTO)) is True


def test_only_stickers_means_no_photo():
    msg = _msg(SNOWLUMA_CUSTOM_FACE, SNOWLUMA_MARKET_FACE_7, NAPCAT_MARKET_FACE)
    assert has_photo(msg) is False


def test_event_dict_is_accepted():
    # 业务层传的是整个事件，不一定要先取出 message
    assert has_photo({"message_type": "group", "message": [SNOWLUMA_PHOTO]}) is True
    assert has_photo({}) is False
    assert has_photo(None) is False


def test_iter_photos_skips_stickers():
    msg = _msg(SNOWLUMA_CUSTOM_FACE, SNOWLUMA_PHOTO, NAPCAT_MARKET_FACE, NAPCAT_PHOTO)
    assert [seg["data"]["file"] for seg in iter_photos(msg)] == ["ABC.jpg", "JKL.jpg"]


def test_photo_urls_skips_sticker_urls():
    # 表情的 URL 不该进 OCR / 图片哈希
    assert photo_urls(_msg(SNOWLUMA_CUSTOM_FACE, SNOWLUMA_PHOTO)) == ["https://x/y"]


# ── 表情名（渲染成 [表情:名字] 时用）────────────────────────────
def test_market_sticker_keeps_its_name():
    assert sticker_name(SNOWLUMA_MARKET_FACE_7) == "呜呜"
    assert sticker_name(NAPCAT_MARKET_FACE) == "唤马"
    assert sticker_name({"type": "mface", "data": {"summary": "[嘬嘬]"}}) == "嘬嘬"


def test_generic_sticker_name_is_dropped():
    """「[动画表情]」是统称，不是名字 —— 渲染成 [表情] 就够了。"""
    assert sticker_name(SNOWLUMA_CUSTOM_FACE) == ""
    assert sticker_name({"type": "image", "data": {"sub_type": 1, "summary": ""}}) == ""
    assert sticker_name({"type": "mface", "data": {"summary": "[贴纸]"}}) == ""


def test_photo_has_no_sticker_name():
    assert sticker_name(SNOWLUMA_PHOTO) == ""
    assert sticker_name({"type": "text", "data": {"text": "hi"}}) == ""


def test_overlong_sticker_name_is_clipped():
    """表情名是用户起的，偶尔是一整句话 —— 别让它占满上下文。"""
    from AbstractionLayer.segments import STICKER_NAME_MAX

    long_name = "[" + "很长的表情名字" * 5 + "]"
    name = sticker_name({"type": "image", "data": {"sub_type": 7, "summary": long_name}})
    assert len(name) == STICKER_NAME_MAX


# ── 发送方向：file 段补 name ─────────────────────────────────────

@pytest.mark.parametrize(
    "source, expected",
    [
        # 线上真实样本：WebUI 上传到临时目录的 Windows 路径
        (
            r"C:\Users\10130\AppData\Local\Temp\webui-upload\89a55d__123.py",
            "89a55d__123.py",
        ),
        ("C:/Users/a/350234.pdf", "350234.pdf"),
        (r"\\server\share\a.pdf", "a.pdf"),
        ("/home/bot/350234.pdf", "350234.pdf"),
        ("file:///C:/Users/a/350234.pdf", "350234.pdf"),
        ("https://example.com/files/350234.pdf?x=1", "350234.pdf"),
        ("350234.pdf", "350234.pdf"),
        ("base64://AAAA", ""),
        ("data:application/pdf;base64,AAAA", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_file_name_from_source(source, expected):
    assert file_name_from_source(source) == expected


def test_normalize_outgoing_adds_name_for_file_only():
    pdf = {"type": "file", "data": {"file": r"C:\tmp\350234.pdf"}}
    image = {"type": "image", "data": {"file": r"C:\tmp\a.jpg"}}

    out = normalize_outgoing([pdf, image])

    assert out[0]["data"]["name"] == "350234.pdf"
    assert "name" not in out[1]["data"]
    # 不修改调用方传进来的段
    assert "name" not in pdf["data"]


def test_normalize_outgoing_keeps_explicit_name():
    seg = {"type": "file", "data": {"file": r"C:\tmp\a.pdf", "name": "报告.pdf"}}
    msg = [seg]
    assert normalize_outgoing(msg) is msg
    assert msg[0]["data"]["name"] == "报告.pdf"


def test_normalize_outgoing_ignores_non_message_values():
    for value in (None, "not-a-list", {"message": []}):
        assert normalize_outgoing(value) is value
