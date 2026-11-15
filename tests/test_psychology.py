"""心理分析：关键词唤醒 / 素材渲染 / 三档图片策略 / 冷却。

模型调用全部用替身，不碰网络；图片用 Pillow 现造，不依赖 data/ 里的文件。
"""

from __future__ import annotations

import io
import re
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.psychology import (
    PsychologyAnalyzer,
    PsychologyUnavailable,
    PsychEntry,
    PsychImage,
    PsychMaterial,
    parse_node_lines,
)
from bot.services.psychology import PsychologyService


# ══════════════════════ 替身 ══════════════════════
class _Settings:
    def __init__(self, data: dict):
        self.data = data

    def section(self, name: str) -> dict:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}


class _FakeLLM:
    """按 system prompt 区分三种任务：视觉描述 / 逐条侧写 / 一段总结。"""

    def __init__(self, calls: list):
        self.calls = calls

    async def ainvoke(self, messages):
        self.calls.append(messages)
        system = str(getattr(messages[0], "content", ""))
        if "客观描述" in system:
            return SimpleNamespace(content="一张聊天截图，最后一句是「晚安」")
        if "逐条解读" in system:
            return SimpleNamespace(content="#1 你在想什么<在硬撑，不想说>\n#2 真的吗<不太信>")
        return SimpleNamespace(content="结论：他在硬撑。\n情绪：有点累。\n怎么接：回他一句「睡吧」。")

    def bind(self, **_kwargs):
        return self


class _BadNodesLLM(_FakeLLM):
    """逐条模式故意不按格式回：用来验证退回文本模式。"""

    async def ainvoke(self, messages):
        system = str(getattr(messages[0], "content", ""))
        if "逐条解读" in system:
            self.calls.append(messages)
            return SimpleNamespace(content="我不太会这个")
        return await super().ainvoke(messages)


class _FakeApi:
    def __init__(self):
        self.sent: list[tuple] = []
        self.forwards: list[tuple] = []

    async def send_group_message(self, group_id, message):
        self.sent.append((group_id, message))
        return {"status": "ok"}

    async def send_private_message(self, user_id, message):
        self.sent.append((user_id, message))
        return {"status": "ok"}

    async def send_group_forward_msg(self, group_id, messages, timeout=None):
        self.forwards.append((group_id, messages))
        return {"status": "ok"}

    async def send_private_forward_msg(self, user_id, messages, timeout=None):
        self.forwards.append((user_id, messages))
        return {"status": "ok"}


class _FakeMp:
    user_id = "10000"

    def __init__(self):
        self.connected = 0

    def is_reply(self, event):
        for seg in event.get("message") or []:
            if seg.get("type") == "reply":
                return (seg.get("data") or {}).get("id")
        return None

    async def ensure_connected(self):
        self.connected += 1

    async def ocr_text(self, url):
        return "图片里的字"


def _config(**overrides) -> dict:
    base = {
        "enable": True,
        "keywords": ["分析心理", "心理分析"],
        "image_mode": "vision",
        "model": "analyse-model",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "max_images": 3,
        "vision": {},
    }
    base.update(overrides)
    return {
        "psychology": base,
        "agent": {
            "models": [],
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model": "main-model",
        },
    }


def _png(color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, "PNG")
    return buffer.getvalue()


def _event(text: str = "分析心理", *, message_type: str = "group") -> dict:
    event = {
        "post_type": "message",
        "message_type": message_type,
        "message_id": 900,
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"nickname": "小明", "user_id": 10001},
    }
    if message_type == "group":
        event["group_id"] = 20001
    return event


def _entry(index: int, text: str, **overrides) -> PsychEntry:
    values = {
        "index": index,
        "sender_id": "10001",
        "sender_name": "小明",
        "time": 1700000000,
        "text": text,
    }
    values.update(overrides)
    return PsychEntry(**values)


def _material(images: list[PsychImage] | None = None, entries=None) -> PsychMaterial:
    return PsychMaterial(
        source="reply_record",
        title="被回复的聊天记录（2 条）",
        transcript="[23:10] 小明（10001）：我没事\n[23:11] 小明（10001）：[图片1]",
        images=images or [],
        entries=(
            entries
            if entries is not None
            else [_entry(1, "我没事"), _entry(2, "[图片1]", image_indexes=[1])]
        ),
    )


# ══════════════════════ 关键词 ══════════════════════
def test_keyword_pattern_matches_whole_message_only():
    service = PsychologyService(mp=_FakeMp(), settings=_Settings(_config()))
    pattern = service.keyword_pattern()
    assert pattern is not None
    regex = pattern["regex"]

    assert re.search(regex, "分析心理", re.I)
    assert re.search(regex, "  心理分析！ ", re.I)
    assert re.search(regex, "心理侧写。", re.I) is None  # 默认词表里没有这个
    # 聊天里顺口提到，不该把 Bot 拽出来
    assert not re.search(regex, "帮我分析心理学", re.I)
    assert not re.search(regex, "分析心理一下这段", re.I)


def test_keyword_pattern_disabled_is_not_registered():
    service = PsychologyService(mp=_FakeMp(), settings=_Settings(_config(enable=False)))
    assert service.keyword_pattern() is None


# ══════════════════════ 素材渲染 ══════════════════════
def test_material_renders_sender_images_and_stickers():
    service = PsychologyService(mp=_FakeMp(), settings=_Settings(_config()))
    record = {
        "message_id": 1,
        "sender": {"nickname": "小明", "user_id": 10001},
        "time": 1700000000,
        "message": [
            {"type": "text", "data": {"text": "我没事"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg", "sub_type": 0}},
            {"type": "image", "data": {"file": "b.gif", "summary": "[动画表情]", "sub_type": 1}},
        ],
    }
    target = SimpleNamespace(
        messages=[record],
        source="reply_single",
        title="被回复的消息",
        record_event=record,
        truncated=False,
    )
    material = service._build_material(target)

    assert "小明（10001）：我没事[图片1][表情]" in material.transcript
    assert len(material.images) == 1  # 表情不算图片
    assert material.images[0].label == "#1"
    assert material.images[0].sender == "小明（10001）"


def test_material_uses_content_key_from_some_backends():
    service = PsychologyService(mp=_FakeMp(), settings=_Settings(_config()))
    record = {
        "message_id": 2,
        "sender": {"nickname": "阿花", "user_id": 10002},
        "content": [{"type": "text", "data": {"text": "晚安"}}],
    }
    target = SimpleNamespace(
        messages=[record], source="reply_single", title="", record_event=record, truncated=False
    )
    material = service._build_material(target)
    assert "阿花（10002）：晚安" in material.transcript


# ══════════════════════ 三档图片策略 ══════════════════════
@pytest.mark.asyncio
async def test_analyzer_image_mode_attaches_image():
    settings = _Settings(_config(image_mode="image"))
    calls: list = []
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))
    image = PsychImage(label="#1", data=_png(), url="http://x/a.jpg", sender="小明")

    answer = await analyzer.analyze(_material([image]))

    assert "结论" in answer
    assert len(calls) == 1
    content = calls[0][1].content
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)
    assert "随消息附上 1 张图片" in content[0]["text"]


@pytest.mark.asyncio
async def test_analyzer_ocr_mode_sends_only_text():
    settings = _Settings(_config(image_mode="ocr"))
    calls: list = []
    analyzer = PsychologyAnalyzer(
        settings, ocr_text=lambda url: _ocr(url), chat_factory=lambda _cfg: _FakeLLM(calls)
    )
    image = PsychImage(label="#1", data=_png(), url="http://x/a.jpg", sender="小明")

    await analyzer.analyze(_material([image]))

    assert len(calls) == 1
    content = calls[0][1].content
    assert isinstance(content, str)
    assert "图片里的字" in content
    assert "image_url" not in content


@pytest.mark.asyncio
async def test_analyzer_vision_mode_describes_before_analysis():
    settings = _Settings(_config(image_mode="vision"))
    calls: list = []
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))
    image = PsychImage(label="#1", data=_png(), url="http://x/a.jpg", sender="小明")

    await analyzer.analyze(_material([image]))

    assert len(calls) == 2  # 先描述、再分析
    second = calls[1][1].content
    assert "图片内容描述" in str(second)
    assert "晚安" in str(second)


@pytest.mark.asyncio
async def test_analyzer_skips_blocked_images():
    settings = _Settings(_config(image_mode="image"))
    calls: list = []
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))
    blocked = PsychImage(label="#1", data=_png(), url="http://x/a.jpg", blocked=True)

    await analyzer.analyze(_material([blocked]))

    content = calls[0][1].content
    assert isinstance(content, str)
    assert "image_url" not in content


@pytest.mark.asyncio
async def test_analyzer_unavailable_reports_missing_fields():
    settings = _Settings({"psychology": {"enable": True}, "agent": {}})
    analyzer = PsychologyAnalyzer(settings)
    assert not analyzer.available
    assert "未配置" in analyzer.status()

    with pytest.raises(PsychologyUnavailable) as info:
        await analyzer.analyze(_material())
    assert "还没配好" in str(info.value)


def test_analyzer_model_id_reads_agent_models():
    data = _config(model_id="m1", model="")
    data["agent"]["models"] = [
        {
            "id": "m1",
            "name": "视觉主力",
            "model": "qwen-vl-max",
            "base_url": "https://dashscope.example.com/v1",
            "api_key": "sk-vl",
        }
    ]
    analyzer = PsychologyAnalyzer(_Settings(data))
    assert analyzer.available
    assert analyzer.config["analysis"].model == "qwen-vl-max"
    assert "视觉主力" in analyzer.status()


# ══════════════════════ 业务编排 ══════════════════════
@pytest.mark.asyncio
async def test_bare_command_replies_usage():
    api = _FakeApi()
    service = PsychologyService(mp=_FakeMp(), api=api, settings=_Settings(_config()))

    await service.run(_event("分析心理"))

    assert api.sent, "裸命令应该回一句用法提示"
    message = api.sent[0][1]
    assert message[0]["type"] == "reply"
    assert "用法" in message[1]["data"]["text"]


@pytest.mark.asyncio
async def test_cooldown_skips_second_trigger():
    api = _FakeApi()
    service = PsychologyService(mp=_FakeMp(), api=api, settings=_Settings(_config()))
    service._last_run["group_20001"] = time.monotonic()

    await service.run(_event("分析心理"))

    assert api.sent == []


@pytest.mark.asyncio
async def test_disabled_group_is_skipped():
    api = _FakeApi()
    service = PsychologyService(
        mp=_FakeMp(),
        api=api,
        settings=_Settings(_config(groups=["99999"])),
    )

    await service.run(_event("分析心理"))

    assert api.sent == []


@pytest.mark.asyncio
async def test_reply_record_is_expanded_and_analyzed():
    """完整链路：回复聊天记录 → 单层展开 → 交给分析器 → 引用回复。"""

    class _Api(_FakeApi):
        kernel = None

        async def get_message(self, message_id):
            return {
                "data": {
                    "message_id": 800,
                    "sender": {"nickname": "小明", "user_id": 10001},
                    "message": [{"type": "forward", "data": {"id": "fw1"}}],
                }
            }

        async def get_forward_msg(self, payload):
            assert payload == {"message_id": "fw1"}
            return {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "sender": {"nickname": "小明", "user_id": 10001},
                            "time": 1700000000,
                            "message": [{"type": "text", "data": {"text": "我没事"}}],
                        },
                        {
                            "sender": {"nickname": "阿花", "user_id": 10002},
                            "time": 1700000060,
                            "message": [{"type": "text", "data": {"text": "真的吗"}}],
                        },
                    ]
                },
            }

    class _Analyzer:
        def __init__(self):
            self.materials = []

        def reload(self):
            pass

        async def analyze(self, material):
            self.materials.append(material)
            return "结论：两个人在互相试探。"

    api = _Api()
    analyzer = _Analyzer()
    service = PsychologyService(
        mp=_FakeMp(),
        api=api,
        settings=_Settings(_config(reply_mode="text")),
        analyzer=analyzer,
    )
    event = _event()
    event["message"] = [
        {"type": "reply", "data": {"id": "800"}},
        {"type": "text", "data": {"text": "分析心理"}},
    ]

    await service.run(event)

    assert analyzer.materials, "应该把素材交给分析器"
    material = analyzer.materials[0]
    assert material.source == "reply_record"
    assert "小明（10001）：我没事" in material.transcript
    assert "阿花（10002）：真的吗" in material.transcript
    assert api.sent, "分析完应该回复"
    reply = api.sent[0][1]
    assert reply[0] == {"type": "reply", "data": {"id": "900"}}
    assert "结论" in reply[1]["data"]["text"]


# ══════════════════════ 逐条侧写 / 聊天记录发送 ══════════════════════
def test_parse_node_lines_keeps_continuation_and_skips_unknown():
    entries = [_entry(1, "a"), _entry(2, "b"), _entry(3, "c")]
    raw = (
        "#2 不是<否定>\n"
        "#9 这条编号不存在\n"
        "#1 @x 你买的？<想知道>\n"
        "#3 <回复消息<我买杯子干什么>>\n"
        "有道理\n"
        "<开玩笑，附和>"
    )
    drafts = parse_node_lines(raw, entries)

    assert drafts[0].text == "@x 你买的？<想知道>"
    assert drafts[1].text == "不是<否定>"
    assert drafts[2].text == "<回复消息<我买杯子干什么>>\n有道理\n<开玩笑，附和>"
    assert all(draft.from_model for draft in drafts)


def test_parse_node_lines_falls_back_to_loose_numbering():
    entries = [_entry(1, "a"), _entry(2, "b")]
    drafts = parse_node_lines("1：不是<否定>\n2. 真的吗<不信>", entries)

    assert drafts[0].text == "不是<否定>"
    assert drafts[1].text == "真的吗<不信>"


def test_parse_node_lines_uses_original_for_missing_entries():
    entries = [_entry(1, "原文一"), _entry(2, "原文二")]
    drafts = parse_node_lines("#1 有分析", entries)

    assert drafts[0].text == "有分析"
    assert drafts[1].text == "原文二"
    assert drafts[1].from_model is False


@pytest.mark.asyncio
async def test_analyze_nodes_returns_per_message_text():
    settings = _Settings(_config(reply_mode="forward"))
    calls: list = []
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))

    drafts = await analyzer.analyze_nodes(_material())

    assert len(drafts) == 2
    assert "在硬撑" in drafts[0].text
    assert len(calls) == 1
    # 逐条模式要把编号清单发给模型，否则对不上
    assert "#1" in calls[0][1].content


@pytest.mark.asyncio
async def test_forward_mode_sends_chat_record():
    api = _FakeApi()
    calls: list = []
    settings = _Settings(_config(reply_mode="forward"))
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))
    service = PsychologyService(
        mp=_FakeMp(), api=api, settings=settings, analyzer=analyzer
    )

    await service.run(_event("分析心理 你在想什么"))

    assert api.forwards, "聊天记录模式应该走 send_group_forward_msg"
    nodes = api.forwards[0][1]
    assert len(nodes) == 1
    node = nodes[0]["data"]
    assert node["uin"] == "10001"
    assert node["name"] == "小明"
    assert "在硬撑" in node["content"][0]["data"]["text"]


@pytest.mark.asyncio
async def test_forward_mode_falls_back_to_text_when_model_ignores_format():
    api = _FakeApi()
    calls: list = []
    settings = _Settings(_config(reply_mode="forward"))
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _BadNodesLLM(calls))
    service = PsychologyService(
        mp=_FakeMp(), api=api, settings=settings, analyzer=analyzer
    )

    await service.run(_event("分析心理 你在想什么"))

    assert api.forwards == []
    texts = [message[-1]["data"]["text"] for _, message in api.sent]
    assert any("改成一段侧写" in text for text in texts), texts
    assert any("结论" in text for text in texts), texts


@pytest.mark.asyncio
async def test_text_mode_does_not_send_forward():
    api = _FakeApi()
    calls: list = []
    settings = _Settings(_config(reply_mode="text"))
    analyzer = PsychologyAnalyzer(settings, chat_factory=lambda _cfg: _FakeLLM(calls))
    service = PsychologyService(
        mp=_FakeMp(), api=api, settings=settings, analyzer=analyzer
    )

    await service.run(_event("分析心理 你在想什么"))

    assert api.forwards == []
    texts = [message[-1]["data"]["text"] for _, message in api.sent]
    assert any("结论" in text for text in texts), texts


async def _ocr(url: str) -> str:
    return "图片里的字"
