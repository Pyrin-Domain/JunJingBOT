"""表情包：库加载 / 名字匹配 / CQ 码转义 / 消息段转换 / 输出解析。"""

import asyncio
import json
from pathlib import Path

import pytest

from messaging import meme as meme_mod
from messaging.meme import MemeLibrary, configure, cq_escape, reset, to_segments


SAMPLE = {
    "faces": [
        {"name": "赞", "describe": "赞同、夸人", "data": {"id": "76"}},
        {"name": "问号", "describe": "疑惑、没看懂", "data": {"id": "10068"}},
    ],
    "mfaces": [
        {
            "name": "嘬嘬",
            "describe": "凑近亲一口",
            "data": {
                "emoji_package_id": 245590,
                "emoji_id": "58a348c57fe9fa6ea0f2c31ea717f7f0",
                "key": "fdcbb9d0e86eb2ba",
                "summary": "[嘬嘬]",
            },
        },
        {
            "name": "笑哭",
            "describe": "好笑、绷不住了",
            "data": {
                "emoji_package_id": 246079,
                "emoji_id": "b8dade658f73cdf2cb7ec639a7d43ac6",
                "key": "d85210f5e6dedd4b",
                "summary": "[笑哭]",
            },
        },
    ],
    "stickers": [],
}


EMOTION_SAMPLE = {
    "faces": [
        {"name": "逆天", "describe": "无语、离谱", "data": {"id": "265"}},
        {"name": "问号", "describe": "疑惑、没看懂", "data": {"id": "10068"}},
        {"name": "捂脸", "describe": "害羞、没眼看", "data": {"id": "264"}},
    ],
    "mfaces": [
        {
            "name": "抱抱",
            "describe": "抱抱、撒娇",
            "data": {
                "emoji_package_id": 240586,
                "emoji_id": "13fa44ff64e6a51bc59ffe471e566d74",
                "key": "1385ebd50773fb55",
                "summary": "[抱抱]",
            },
        },
        {
            "name": "摸头",
            "describe": "安慰、摸摸头",
            "data": {
                "emoji_package_id": 243620,
                "emoji_id": "c43c011c067ba900e4625e5ce1b200a9",
                "key": "9acf42d1fb2a1d73",
                "summary": "[摸头]",
            },
        },
        {
            "name": "震惊",
            "describe": "震惊、真的假的",
            "data": {
                "emoji_package_id": 240586,
                "emoji_id": "ab0c10ac330c208517d9e62392c9a834",
                "key": "1385ebd50773fb55",
                "summary": "[震惊]",
            },
        },
    ],
    "stickers": [],
}


@pytest.fixture()
def library(tmp_path):
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    return MemeLibrary.load(file)


@pytest.fixture()
def emotion_library(tmp_path):
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(EMOTION_SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file)
    return file


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset()
    yield
    reset()


# ════════════════════ 库 ════════════════════
def test_load_reads_faces_and_mfaces(library):
    assert len(library) == 4
    assert {entry.kind for entry in library.entries} == {"face", "mface"}


def test_load_missing_file_is_empty(tmp_path):
    assert len(MemeLibrary.load(tmp_path / "nope.json")) == 0


def test_load_broken_json_is_empty(tmp_path):
    file = tmp_path / "bad.json"
    file.write_text("{not json", encoding="utf-8")
    assert len(MemeLibrary.load(file)) == 0


def test_exact_name_wins(library):
    assert library.find("嘬嘬").name == "嘬嘬"


def test_name_with_noise_words_still_hits(library):
    """模型爱写「嘬嘬表情」「【嘬嘬】」这种，不能因此判成不存在。"""
    for query in ("嘬嘬表情", "【嘬嘬】", " 嘬嘬 "):
        assert library.find(query).name == "嘬嘬"


def test_partial_name_falls_back(library):
    assert library.find("嘬").name == "嘬嘬"


def test_describe_keyword_falls_back(library):
    """模型没记住名字、只写了情绪时，按描述兜一层。"""
    assert library.find("好笑").name == "笑哭"


def test_unknown_name_returns_none(library):
    assert library.find("完全不存在的表情") is None
    assert library.find("") is None


def test_catalog_lines_use_name_and_describe(library):
    lines = library.catalog()
    assert "嘬嘬：凑近亲一口" in lines
    assert all("：" in line for line in lines)


# ════════════════════ CQ 码 ════════════════════
def test_mface_cq_escapes_brackets_in_summary(library):
    code = library.cq("嘬嘬")
    assert code.startswith("[CQ:mface,")
    # summary 的方括号必须转义，否则 CQ 码会被提前截断
    assert "summary=&#91;嘬嘬&#93;" in code
    assert code.count("[") == 1 and code.count("]") == 1


def test_face_cq_uses_id(library):
    assert library.cq("赞") == "[CQ:face,id=76]"


def test_cq_escape_handles_separators():
    assert cq_escape("[a,b]&c") == "&#91;a&#44;b&#93;&amp;c"


def test_unknown_name_gives_empty_cq(library):
    assert library.cq("没有这个") == ""


# ════════════════════ CQ → 消息段 ════════════════════
def test_plain_text_returns_none(library):
    """没有表情包的普通消息不转段，旧发送路径完全不动。"""
    assert to_segments("今天吃什么") is None
    assert to_segments("") is None


def test_text_and_mface_split_in_order(library):
    segments = to_segments(f"笑死 {library.cq('嘬嘬')}")
    assert [seg["type"] for seg in segments] == ["text", "mface"]
    assert segments[0]["data"]["text"] == "笑死 "
    # 段数组里的 summary 是还原后的原文，不带实体；package_id 还原成数字
    assert segments[1]["data"]["summary"] == "[嘬嘬]"
    assert segments[1]["data"]["emoji_package_id"] == 245590


def test_reply_and_mface_keep_both_segments(library):
    segments = to_segments(f"[CQ:reply,id=12345]{library.cq('笑哭')}")
    assert [seg["type"] for seg in segments] == ["reply", "mface"]
    assert segments[0]["data"] == {"id": "12345"}


def test_whitespace_only_mface_dropped(library):
    """纯空白 + 表情包是可以发的；但整条都空时不能发空消息。"""
    assert to_segments("  ") is None


# ════════════════════ 输出解析 ════════════════════
def test_parse_renders_meme_tag(tmp_path):
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file)

    from agent.output import parse_agent_output

    parsed = parse_agent_output(
        "<output><message>这也行？<meme>问号</meme></message></output>"
    )
    assert parsed["messages"] == ["这也行？[CQ:face,id=10068]"]


def test_parse_drops_unknown_meme_name(tmp_path):
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file)

    from agent.output import parse_agent_output

    parsed = parse_agent_output("<output><message>哈啊<meme>瞎编的名字</meme></message></output>")
    assert parsed["messages"] == ["哈啊"]


def test_parse_attaches_meme_written_outside_message(tmp_path):
    """模型把 <meme> 写在 </message> 后面时，也要补进这条消息，不能丢掉。"""
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file)

    from agent.output import parse_agent_output

    parsed = parse_agent_output(
        "<output><message>哈啊</message><meme>问号</meme></output>"
    )
    assert parsed["messages"] == ["哈啊[CQ:face,id=10068]"]


def test_parse_does_not_double_meme(tmp_path):
    """消息里已经有一个表情时，外面再写一个也不要重复发。"""
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file)

    from agent.output import parse_agent_output

    parsed = parse_agent_output(
        "<output><message>哈啊<meme>问号</meme></message><meme>问号</meme></output>"
    )
    assert parsed["messages"] == ["哈啊[CQ:face,id=10068]"]


def test_parse_removes_meme_tag_when_disabled(tmp_path):
    file = tmp_path / "memes.json"
    file.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    configure(file, enable=False)

    from agent.output import parse_agent_output

    parsed = parse_agent_output("<output><message>哈啊<meme>嘬嘬</meme></message></output>")
    assert parsed["messages"] == ["哈啊"]


# ════════════════════ 发送 ════════════════════
class _FakeAPI:
    def __init__(self):
        self.sent = []

    async def send_group_message(self, group_id, message):
        self.sent.append(("group", group_id, message))
        return {"message_id": 1}

    async def send_private_message(self, user_id, message):
        self.sent.append(("private", user_id, message))
        return {"message_id": 2}


class _FakeImages:
    def resolve(self, addr, summary=""):
        return type("Ref", (), {"napcat_path": addr})()


def _processor(api):
    from messaging.processor import MessageProcessor

    return MessageProcessor(api=api, images=_FakeImages(), thread_history=None)


def test_send_message_converts_mface_to_segments(library):
    api = _FakeAPI()
    mp = _processor(api)
    asyncio.run(
        mp.send_message(
            {"message_type": "group", "group_id": 123}, f"笑死 {library.cq('嘬嘬')}"
        )
    )
    _, group_id, payload = api.sent[0]
    assert group_id == 123
    assert [seg["type"] for seg in payload] == ["text", "mface"]
    assert payload[0]["data"]["text"] == "笑死 "


def test_send_message_keeps_plain_text_as_is():
    api = _FakeAPI()
    mp = _processor(api)
    asyncio.run(mp.send_message({"message_type": "group", "group_id": 123}, "普通一句话"))
    assert api.sent[0][2] == "普通一句话"


def test_send_message_private_also_converted(library):
    api = _FakeAPI()
    mp = _processor(api)
    asyncio.run(
        mp.send_message({"message_type": "private", "user_id": 999}, library.cq("笑哭"))
    )
    kind, user_id, payload = api.sent[0]
    assert (kind, user_id) == ("private", 999)
    assert isinstance(payload, list) and payload[0]["type"] == "mface"


# ════════════════════ 上下文渲染 ════════════════════
def test_mface_segment_renders_with_name():
    """别人发的表情包要带名字进上下文，模型才知道群里常用哪几个。"""
    from messaging.models import parse_event, render_body

    msg = parse_event(
        {
            "message_id": "9",
            "user_id": "1",
            "message": [
                {"type": "mface", "data": {"summary": "[嘬嘬]", "emoji_id": "x"}},
            ],
        }
    )
    assert render_body(msg) == "[表情:嘬嘬]"


def test_mface_without_summary_renders_placeholder():
    from messaging.models import parse_event, render_body

    msg = parse_event(
        {"message_id": "9", "user_id": "1", "message": [{"type": "mface", "data": {}}]}
    )
    assert render_body(msg) == "[表情]"


# ════════════════════ 情绪提示 ════════════════════
def test_meme_hint_for_comfort(emotion_library):
    """求安慰时点名一个真实存在的安慰类表情，避免模型只回文字。"""
    from agent.hints import build_meme_hint

    hint = build_meme_hint("呜呜呜，今天好累，想被安慰")
    assert "安慰" in hint
    assert "<meme>名字</meme>" in hint
    assert "摸头" in hint or "抱抱" in hint
    # 不能建议模型写方括号伪表情
    assert "别写 `[表情:名字]`" in hint


def test_meme_hint_for_hug(emotion_library):
    from agent.hints import build_meme_hint

    hint = build_meme_hint("抱抱")
    assert "撒娇" in hint and "抱抱" in hint


def test_meme_hint_for_absurd(emotion_library):
    from agent.hints import build_meme_hint

    hint = build_meme_hint("这也太离谱了吧")
    assert "无语" in hint and "逆天" in hint


def test_meme_hint_for_shy(emotion_library):
    from agent.hints import build_meme_hint

    hint = build_meme_hint("骗人，眼睛都红了")
    assert "害羞" in hint and "捂脸" in hint


def test_meme_hint_for_question(emotion_library):
    from agent.hints import build_meme_hint

    hint = build_meme_hint("你说是不是")
    assert "疑惑" in hint and "问号" in hint


def test_meme_hint_for_explicit_request(emotion_library):
    from agent.hints import build_meme_hint

    hint = build_meme_hint("发个表情包")
    assert "必须真的发一个" in hint
    assert "<meme>名字</meme>" in hint


def test_meme_hint_empty_for_neutral_text(emotion_library):
    from agent.hints import build_meme_hint

    assert build_meme_hint("今天天气不错") == ""
    assert build_meme_hint("") == ""


def test_meme_hint_disabled(emotion_library):
    from agent.hints import build_meme_hint

    configure(emotion_library, enable=False)
    assert build_meme_hint("呜呜呜") == ""


def test_meme_hint_skips_names_missing_from_library(tmp_path):
    """候选名字不在库里时不要硬编，宁可这次不提示。"""
    from agent.hints import build_meme_hint

    file = tmp_path / "other.json"
    file.write_text(
        json.dumps(
            {
                "faces": [],
                "mfaces": [
                    {
                        "name": "完全无关",
                        "describe": "别的东西",
                        "data": {
                            "emoji_package_id": 1,
                            "emoji_id": "x",
                            "key": "y",
                            "summary": "[x]",
                        },
                    }
                ],
                "stickers": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    configure(file)
    assert build_meme_hint("呜呜呜") == ""
