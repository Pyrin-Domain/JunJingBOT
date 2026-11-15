"""``is_contain_image`` 的判据。

为什么单独测：它决定「机器人图片搬运」「img_fd 批量转发」这些功能开不开火。
旧判据是「``summary`` 为空才算图片」，而 SnowLuma / 新版 NapCat 给普通图片的
``summary`` 就是 ``"[图片]"``（实测 ``sub_type=0``），于是判据恒为假 ——
功能看起来「配了但没反应」。
"""

from __future__ import annotations

from messaging.processor import MessageProcessor

# 不走 __init__：这个判定是纯函数，不需要连接 / 配置
MP = MessageProcessor.__new__(MessageProcessor)


def _event(*segments) -> dict:
    return {"message": list(segments)}


def _image(summary="[图片]", sub_type=0) -> dict:
    return {
        "type": "image",
        "data": {"file": "a.jpg", "summary": summary, "sub_type": sub_type},
    }


def test_real_image_with_summary_is_detected():
    # 真机上就是这个形状：summary="[图片]"、sub_type=0
    assert MP.is_contain_image(_event(_image())) is True


def test_image_without_summary_is_still_detected():
    assert MP.is_contain_image(_event(_image(summary=""))) is True


def test_sticker_sub_type_is_ignored():
    assert MP.is_contain_image(_event(_image(sub_type=1))) is False


def test_animated_face_summary_is_ignored():
    assert MP.is_contain_image(_event(_image(summary="[动画表情]"))) is False


def test_market_face_sub_type_7_is_ignored():
    # 真机样本：商城表情 sub_type=7、summary 直接是表情名 "[呜呜]"
    assert MP.is_contain_image(_event(_image(summary="[呜呜]", sub_type=7))) is False
    assert MP.is_contain_image(_event(_image(summary="[emo]", sub_type=7))) is False


def test_market_face_without_sub_type_is_ignored():
    # 兜底：协议端没给 sub_type 时，靠 summary 的 "[名字]" 形式识别表情
    assert MP.is_contain_image(_event(_image(summary="[嘬嘬]", sub_type=0))) is False
    assert MP.is_contain_image(_event(_image(summary="[emo]"))) is False


def test_image_with_filename_summary_is_detected():
    # 个别协议端把 summary 填成文件名，不能因此丢掉真图片
    assert MP.is_contain_image(_event(_image(summary="IMG_0042.jpg"))) is True


def test_sticker_before_real_image_still_detects_image():
    event = _event(_image(summary="[动画表情]", sub_type=1), _image())
    assert MP.is_contain_image(event) is True


def test_only_stickers_means_no_image():
    event = _event(
        _image(summary="[动画表情]", sub_type=1),
        _image(summary="[呜呜]", sub_type=7),
        {"type": "mface", "data": {"summary": "[嘬嘬]"}},
    )
    assert MP.is_contain_image(event) is False


# ── NapCat 形状（字段名与 SnowLuma 不同，必须同样成立）──────────────


def _napcat_image(**extra) -> dict:
    """NapCat 接收时的 image 段：会多带 file_size，表情则多带 emoji 三字段。"""
    data = {"file": "ABC.jpg", "url": "https://x/y", "summary": "[图片]", "sub_type": 0, "file_size": 10240}
    data.update(extra)
    return {"type": "image", "data": data}


def test_napcat_plain_image_is_detected():
    assert MP.is_contain_image(_event(_napcat_image())) is True
    # NapCat 普通图片的 summary 也可能是空串
    assert MP.is_contain_image(_event(_napcat_image(summary=""))) is True


def test_napcat_market_sticker_is_ignored():
    # 官方文档：商城表情仍是 image 段，带 key / emoji_id / emoji_package_id，
    # 且没承诺 sub_type 会是非 0 —— 只看 sub_type 会漏。
    sticker = _napcat_image(
        summary="[emo]",
        sub_type=0,
        emoji_id="abc123",
        emoji_package_id="1",
        key="k",
    )
    assert MP.is_contain_image(_event(sticker)) is False
    # 连 summary 都空掉也要能认出来
    assert MP.is_contain_image(_event(_napcat_image(summary="", emoji_id="abc123"))) is False
    assert MP.is_contain_image(_event(_napcat_image(summary="", key="k"))) is False


def test_napcat_custom_face_is_ignored():
    assert MP.is_contain_image(_event(_napcat_image(summary="", sub_type=1))) is False
    assert MP.is_contain_image(_event(_napcat_image(summary="[动画表情]", sub_type=1))) is False


def test_sub_type_as_string_is_handled():
    # 个别协议端把 sub_type 序列化成字符串
    assert MP.is_contain_image(_event(_napcat_image(sub_type="1"))) is False
    assert MP.is_contain_image(_event(_napcat_image(sub_type="0"))) is True
    assert MP.is_contain_image(_event(_napcat_image(sub_type="abc"))) is True


def test_napcat_sticker_before_image_still_detects_image():
    event = _event(_napcat_image(summary="", emoji_id="abc123"), _napcat_image())
    assert MP.is_contain_image(event) is True


def test_mface_is_not_an_image():
    event = _event({"type": "mface", "data": {"summary": "[嘬嘬]"}})
    assert MP.is_contain_image(event) is False


def test_text_only_message_has_no_image():
    assert MP.is_contain_image(_event({"type": "text", "data": {"text": "hi"}})) is False


def test_empty_event():
    assert MP.is_contain_image({}) is False
    assert MP.is_contain_image(None) is False


def test_mixed_message_counts_as_image():
    event = _event(
        {"type": "text", "data": {"text": "看图"}},
        _image(),
    )
    assert MP.is_contain_image(event) is True
