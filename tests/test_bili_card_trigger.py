"""「回复卡片 → 下载」这条链路的卡片识别测试。

背景：``check_is_miniapp`` 只认 ``com.tencent.miniapp_01``（真·QQ 小程序卡片），
而群里分享 B 站视频产生的是 ``com.tencent.structmsg``（结构化分享卡片）。
用户「回复卡片 + bili_dl」的诉求两种都要能命中，所以另开了
:func:`messaging.forward.get_bili_card_link`。
"""

from __future__ import annotations

import json

import pytest

from messaging.forward import get_bili_card_link, get_miniapp_info


def _event(app: str, meta: dict) -> dict:
    payload = json.dumps({"app": app, "meta": meta}, ensure_ascii=False)
    return {"message": [{"type": "json", "data": {"data": payload}}]}


def test_miniapp_card_is_recognised():
    event = _event(
        "com.tencent.miniapp_01",
        {
            "detail_1": {
                "title": "哔哩哔哩",
                "desc": "视频",
                "qqdocurl": "https://b23.tv/abcd123",
            }
        },
    )
    assert get_bili_card_link(event) == "https://b23.tv/abcd123"


def test_structmsg_news_card_is_recognised():
    """B 站视频分享走 structmsg，链接在 meta.news.jumpUrl。"""
    event = _event(
        "com.tencent.structmsg",
        {
            "news": {
                "title": "某个视频",
                "desc": "up主",
                "jumpUrl": "https://www.bilibili.com/video/BV1xx411c7mD",
            }
        },
    )
    assert get_bili_card_link(event) == "https://www.bilibili.com/video/BV1xx411c7mD"
    # 老实现认不出这种卡片，这正是要补的缺口
    assert get_miniapp_info(event) == {}


def test_structmsg_with_legacy_detail_url():
    event = _event(
        "com.tencent.structmsg",
        {"detail": {"qqdocurl": "https://www.bilibili.com/video/av170001"}},
    )
    assert get_bili_card_link(event) == "https://www.bilibili.com/video/av170001"


def test_non_bilibili_card_is_ignored():
    event = _event(
        "com.tencent.structmsg",
        {"news": {"title": "外卖", "jumpUrl": "https://example.com/order/1"}},
    )
    assert get_bili_card_link(event) == ""


def test_plain_text_message_has_no_card():
    assert get_bili_card_link({"message": [{"type": "text", "data": {"text": "hi"}}]}) == ""


def test_broken_json_does_not_raise():
    event = {"message": [{"type": "json", "data": {"data": "{not json"}}]}
    assert get_bili_card_link(event) == ""


@pytest.mark.parametrize(
    "text,should_match",
    [
        ("bili_dl", True),
        ("  bili_dl", True),
        ("/bili_dl", True),
        ("bili_dl https://b23.tv/abc", True),
        ("帮我 bili_dl 一下", False),
        ("x_bili_dl", False),
    ],
)
def test_command_regex_only_matches_whole_command(text, should_match):
    """``bili_dl`` 必须是开头命令，避免群里聊到这个词就触发下载。"""
    import re

    regex = r"^\s*/?bili_dl\b"
    assert bool(re.search(regex, text, re.I)) is should_match
