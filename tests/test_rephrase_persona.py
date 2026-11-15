"""改写路径必须带人设：改一句复读话不能改出另一个人的语气。

背景：去复读会调「改写」模型，那段提示词只给「对方说 / 你要说的是」两行，
人设整个不在 → 模型退回通用中文，改出来阴阳怪气、没有可爱感
（线上真实案例：「明明是一早上全是图」→「大清早满屏都是图，还嫌我话多？」）。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent.prompts import (
    _INTERJECT_JUDGE_SYSTEM,
    _REPHRASE_SYSTEM,
    persona_voice_excerpt,
)
from agent.runtime import QQBotAgent


class _StubLLM:
    def __init__(self, content="改写后的句子"):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.content)


class _FakeSettings:
    """只要 section("agent") 那一段；路径给绝对路径，免得去找 root。"""

    def __init__(self, persona_file: str, core_file: str = ""):
        self._agent = {
            "persona_file": persona_file,
            "persona_core_file": core_file,
        }

    def section(self, name):
        return self._agent if name == "agent" else {}


# ── 人设摘录 ────────────────────────────────────────────────
def test_voice_excerpt_only_takes_voice_section():
    voice = persona_voice_excerpt()
    assert voice
    assert "接梗" in voice                 # 「说话方式」段
    assert "你的名字叫作" not in voice      # 经历别带进来


def test_voice_excerpt_drops_example_lines():
    """示范台词不能进摘录：改写模型会照抄出来。"""
    voice = persona_voice_excerpt()
    assert "接梗" in voice                 # 规则留着
    assert "怎么这么多大肥鱼" not in voice  # 例句去掉


def test_voice_excerpt_falls_back_without_voice_section(tmp_path: Path):
    persona = tmp_path / "p.md"
    persona.write_text("# 我是谁\n就一句经历，没有说话方式。\n", encoding="utf-8")
    voice = persona_voice_excerpt(_FakeSettings(str(persona)))
    assert voice
    assert "就一句经历" not in voice


# ── 改写时把语气接上 ────────────────────────────────────────
def test_rephrase_prompt_carries_persona_voice():
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.settings = None
    stub = _StubLLM("大清早满屏都是图")
    agent.llm = stub
    agent.reply_router = None

    result = asyncio.run(
        agent._rephrase("明明是一早上全是图", [], user_message="机器人b话怎么这么多")
    )
    assert result == "大清早满屏都是图"
    system = stub.calls[0][0].content
    assert _REPHRASE_SYSTEM in system
    assert "君景说话的方式" in system
    assert "接梗" in system                  # 人设的语气确实进去了


def test_rephrase_system_forbids_adding_attitude():
    """只换说法、不换态度：不让改写顺手加一句抱怨。"""
    assert "不要给原句添加新的抱怨" in _REPHRASE_SYSTEM
    assert "不要改成阴阳怪气" in _REPHRASE_SYSTEM


# ── 插话判断：别太吵 ────────────────────────────────────────
def test_judge_prompt_keeps_away_from_others_conversation():
    prompt = _INTERJECT_JUDGE_SYSTEM
    assert "对方在吐槽别人" in prompt
    assert "沉默就是失职" not in prompt     # 上一版太绝对，会插得很吵
    assert "有人提问没人接、而你答得上 → 该答" in prompt
