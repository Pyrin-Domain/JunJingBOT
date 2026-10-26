"""转发末尾广告裁剪 + 尾部追加（messaging.forward_filter）。

夹具用的是线上真实结构（message_id 64035048，33 条聊天记录，末尾挂
「二维码进群海报 + com.tencent.tuwen.lua 群推荐卡片」），只把图片 url
换成占位符，二维码判定通过 monkeypatch 注入。
"""
import json

import pytest

from messaging import forward_filter as ff


# ── 夹具：真实结构的精简版 ──────────────────────────────
def _text(text, uid=10003, nick="QQ用户"):
    return {
        "user_id": uid,
        "time": 1792736000,
        "message_id": 633967386,
        "sender": {"user_id": uid, "nickname": nick, "card": ""},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _image(url="https://multimedia.nt.qq.com.cn/download?fileid=x", uid=10003, nick="QQ用户"):
    return {
        "user_id": uid,
        "time": 1792736400,
        "message_id": 1915342332,
        "sender": {"user_id": uid, "nickname": nick, "card": ""},
        "message": [{"type": "image", "data": {"summary": "", "file": "x.png", "url": url}}],
    }


TUWEN_CARD = {
    "app": "com.tencent.tuwen.lua",
    "prompt": "欢迎加入艺术站【点我传送👋】",
    "meta": {
        "news": {
            "title": "欢迎加入艺术站【点我传送👋】",
            "desc": "⚡️超多各种乐子共享🍉 点我加入乐子共享小基地！",
            "jumpUrl": "https://m.q.qq.com/a/p/1110034113?s=%2Fpages%2Fabout%2Fabout%3Fid%3D18",
        }
    },
}


def _json_card(payload, uid=10003):
    return {
        "user_id": uid,
        "time": 1792736492,
        "message_id": 1658089773,
        "sender": {"user_id": uid, "nickname": "QQ用户", "card": ""},
        "message": [
            {"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}
        ],
    }


def _forward_tail():
    """线上那 33 条的末尾 4 条：正常图 / 表情 / 二维码海报 / 群推荐卡片。"""
    return [
        _image("https://cdn/legit.png"),      # 正常内容
        _image("https://cdn/emoji.png"),      # 动画表情
        _image("https://cdn/qr_poster.png"),  # 带二维码的进群海报
        _json_card(TUWEN_CARD),               # 群推荐卡片
    ]


@pytest.fixture
def qr_urls(monkeypatch):
    """把「二维码检测」换成按 url 判定，避免测试依赖图片下载。"""
    marked = {"https://cdn/qr_poster.png"}
    monkeypatch.setattr(ff, "image_has_qr", lambda url, **kw: url in marked)
    return marked


# ══════════════════ 配置归一化 ══════════════════
def test_normalize_settings_tolerates_short_forms():
    assert ff.normalize_settings(None) == {}
    assert ff.normalize_settings(False) == {}

    opts = ff.normalize_settings(True)
    assert opts["enable"] is True
    assert opts["scan_tail"] == ff.DEFAULT_SCAN_TAIL
    assert opts["drop_qr"] and opts["drop_group_card"] and opts["drop_link"]
    assert opts["append"] == {"enable": False, "items": []}


def test_normalize_settings_keeps_explicit_values():
    opts = ff.normalize_settings(
        {
            "enable": True,
            "scan_tail": 5,
            "drop_link": False,
            "append": {"enable": True, "items": [{"uin": 1, "name": "n", "content": "x"}]},
        }
    )
    assert opts["scan_tail"] == 5
    assert opts["drop_qr"] is True       # 没写的按默认
    assert opts["drop_link"] is False
    assert len(opts["append"]["items"]) == 1


def test_normalize_settings_accepts_bare_append_list():
    opts = ff.normalize_settings({"append": [{"content": "签名"}]})
    assert opts["append"]["enable"] is True
    assert opts["append"]["items"] == [{"content": "签名"}]


def test_normalize_settings_bad_scan_tail_falls_back():
    assert ff.normalize_settings({"scan_tail": "abc"})["scan_tail"] == ff.DEFAULT_SCAN_TAIL
    assert ff.normalize_settings({"scan_tail": -2})["scan_tail"] == ff.DEFAULT_SCAN_TAIL
    assert ff.normalize_settings({"scan_tail": 0})["scan_tail"] == 0


def test_is_enabled():
    assert ff.is_enabled(None) is False
    assert ff.is_enabled({}) is False
    assert ff.is_enabled(ff.normalize_settings({"enable": False})) is False
    # 只开追加也算启用
    assert ff.is_enabled(ff.normalize_settings({"append": {"enable": True, "items": [{"content": "x"}]}}))
    # 追加开着但列表空、又不裁尾巴 → 没有可做的
    opts = ff.normalize_settings({"scan_tail": 0, "append": {"enable": True, "items": []}})
    assert ff.is_enabled(opts) is False


# ══════════════════ 逐条判定 ══════════════════
def test_group_card_detected_by_app():
    assert ff.classify_message(_json_card(TUWEN_CARD)) == "group_card"


def test_group_card_detected_by_jump_url():
    payload = {
        "app": "com.tencent.structmsg",
        "meta": {"news": {"title": "点我", "jumpUrl": "https://qun.qq.com/join?id=123"}},
    }
    assert ff.classify_message(_json_card(payload)) == "group_card"


def test_bilibili_miniapp_is_not_group_card():
    payload = {
        "app": "com.tencent.miniapp_01",
        "meta": {
            "detail_1": {
                "title": "哔哩哔哩",
                "desc": "【视频】某个视频",
                "qqdocurl": "https://b23.tv/xxx",
            }
        },
    }
    assert ff.classify_message(_json_card(payload)) is None


def test_qr_image_detected(qr_urls):
    assert ff.classify_message(_image("https://cdn/qr_poster.png")) == "qr_image"
    assert ff.classify_message(_image("https://cdn/legit.png")) is None


def test_qr_can_be_turned_off(qr_urls):
    msg = _image("https://cdn/qr_poster.png")
    assert ff.classify_message(msg, drop_qr=False) is None


def test_plain_link():
    assert ff.classify_message(_text("https://m.q.qq.com/a/p/1110034113?s=/pages/about")) == "plain_link"


def test_chat_with_inline_url_is_kept():
    msg = _text("这段剧情我看过 https://www.bilibili.com/video/BV1xx 挺好笑的")
    assert ff.classify_message(msg) is None


def test_link_rule_can_be_turned_off():
    msg = _text("https://example.com/abc")
    assert ff.classify_message(msg, drop_link=False) is None


# ══════════════════ 尾部裁剪 ══════════════════
def test_strip_only_scans_tail(qr_urls):
    messages = [_image("https://cdn/qr_poster.png")] + [_image("https://cdn/legit.png")] * 4
    kept, removed = ff.strip_tail_ads(messages, scan_tail=3)
    # 二维码在开头（第 0 条），不在末尾 3 条里 → 一条都不丢
    assert len(kept) == 5
    assert removed == []


def test_strip_drops_ads_in_tail(qr_urls):
    messages = [_text("谁说国产没有好剧情")] + _forward_tail()
    kept, removed = ff.strip_tail_ads(messages, scan_tail=3)
    assert len(kept) == 3          # 文本 + 两张正常图
    assert [item["reason"] for item in removed] == ["qr_image", "group_card"]
    assert [item["index"] for item in removed] == [3, 4]


def test_strip_scan_tail_zero_keeps_everything(qr_urls):
    messages = [_text("x")] + _forward_tail()
    kept, removed = ff.strip_tail_ads(messages, scan_tail=0)
    assert len(kept) == len(messages)
    assert removed == []


def test_strip_keeps_legit_tail(qr_urls):
    messages = [_image("https://cdn/legit.png"), _text("正常聊天"), _image("https://cdn/legit2.png")]
    kept, removed = ff.strip_tail_ads(messages, scan_tail=3)
    assert len(kept) == 3 and removed == []


# ══════════════════ 重建 node ══════════════════
def test_build_nodes_keeps_sender_and_segments(qr_urls):
    messages = [_text("你好", uid=10003, nick="小明"), _image()]
    nodes = ff.build_nodes(messages)
    assert nodes[0]["type"] == "node"
    assert nodes[0]["data"]["uin"] == "10003"
    assert nodes[0]["data"]["name"] == "小明"
    assert nodes[0]["data"]["content"][0]["data"]["text"] == "你好"
    assert nodes[1]["data"]["content"][0]["type"] == "image"


def test_build_nodes_accepts_legacy_content_key():
    msg = {"user_id": 1, "time": 1, "sender": {"nickname": "旧版"}, "content": [{"type": "text", "data": {"text": "hi"}}]}
    nodes = ff.build_nodes([msg])
    assert nodes[0]["data"]["name"] == "旧版"
    assert nodes[0]["data"]["content"][0]["data"]["text"] == "hi"


# ══════════════════ 追加消息 ══════════════════
def test_build_append_nodes_text_content():
    nodes = ff.build_append_nodes(
        [{"uin": 10004, "name": "N552AA🍥🍜🍣🍕💊🍫", "content": "独角兽君景搬运喵~"}],
        now=1700000000,
    )
    assert len(nodes) == 1
    data = nodes[0]["data"]
    assert data["uin"] == "10004"
    assert data["name"] == "N552AA🍥🍜🍣🍕💊🍫"
    assert data["time"] == 1700000000
    assert data["content"] == [{"type": "text", "data": {"text": "独角兽君景搬运喵~"}}]


def test_build_append_nodes_multiple_items_keep_order():
    items = [{"uin": 1, "name": "a", "content": "第一条"}, {"uin": 1, "name": "a", "content": "第二条"}]
    nodes = ff.build_append_nodes(items)
    assert [n["data"]["content"][0]["data"]["text"] for n in nodes] == ["第一条", "第二条"]


def test_build_append_nodes_inlines_local_image(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    nodes = ff.build_append_nodes(
        [{"uin": 1, "name": "a", "content": [{"type": "image", "data": {"file": str(img)}}]}]
    )
    assert nodes[0]["data"]["content"][0]["data"]["file"].startswith("base64://")


def test_build_append_nodes_keeps_remote_image():
    nodes = ff.build_append_nodes(
        [{"uin": 1, "name": "a", "content": [{"type": "image", "data": {"file": "https://cdn/x.png"}}]}]
    )
    assert nodes[0]["data"]["content"][0]["data"]["file"] == "https://cdn/x.png"


def test_build_append_nodes_skips_empty_content():
    assert ff.build_append_nodes([{"uin": 1, "name": "a", "content": ""}]) == []
    assert ff.build_append_nodes([{"uin": 1, "name": "a"}]) == []
    assert ff.build_append_nodes(["不是对象"]) == []


# ══════════════════ 组合：裁剪 + 追加 ══════════════════
def test_build_forward_nodes_strip_and_append(qr_urls):
    messages = [_text("正文")] + _forward_tail()
    opts = ff.normalize_settings(
        {
            "scan_tail": 3,
            "append": {
                "enable": True,
                "items": [
                    {"uin": 10004, "name": "签名号", "content": "独角兽君景搬运喵~"},
                    {"uin": 10004, "name": "签名号", "content": "第二条"},
                ],
            },
        }
    )
    nodes, report = ff.build_forward_nodes(messages, opts, now=1700000000)
    assert report["total"] == 5
    assert report["kept"] == 3
    assert report["appended"] == 2
    assert report["rebuilt"] is True
    assert [item["reason"] for item in report["removed"]] == ["qr_image", "group_card"]
    # 追加的排在最后
    assert nodes[-1]["data"]["content"][0]["data"]["text"] == "第二条"
    assert nodes[-2]["data"]["content"][0]["data"]["text"] == "独角兽君景搬运喵~"


def test_build_forward_nodes_nothing_to_do_means_no_rebuild(qr_urls):
    messages = [_text("A"), _text("B"), _text("C")]
    nodes, report = ff.build_forward_nodes(messages, ff.normalize_settings(True))
    assert report["removed"] == []
    assert report["appended"] == 0
    assert report["rebuilt"] is False
    assert len(nodes) == 3


def test_build_forward_nodes_append_only(qr_urls):
    messages = [_text("A")]
    opts = ff.normalize_settings({"append": {"enable": True, "items": [{"content": "签名"}]}})
    nodes, report = ff.build_forward_nodes(messages, opts)
    assert report["rebuilt"] is True
    assert report["appended"] == 1
    assert len(nodes) == 2


def test_build_forward_nodes_append_disabled(qr_urls):
    messages = [_text("A")]
    opts = ff.normalize_settings({"append": {"enable": False, "items": [{"content": "签名"}]}})
    nodes, report = ff.build_forward_nodes(messages, opts)
    assert report["appended"] == 0
    assert report["rebuilt"] is False
