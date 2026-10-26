"""群聊插话：累计消息 / 定时器触发、冷却、白名单，以及「先判断再开口」两步走。"""
import asyncio
from types import SimpleNamespace

from agent.runtime import (
    QQBotAgent,
    _INTERJECT_REPLY_HINT,
    _parse_interject_judge,
)
from agent.prompts import interject_hint
from config import BotSettings
from bot.services.interject import InterjectService

import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="junjing_interject_test_"))


def _settings(**overrides):
    return BotSettings(root=_TMP, overrides={"interject": overrides}, auto_create=False)

GROUP_ID = 583261953
SELF_ID = 10002


def _event(seq: int, user_id: int = 10001) -> dict:
    return {
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": user_id,
        "message_id": seq,
        "message_seq": seq,
    }


class FakeMP:
    def __init__(self):
        self.user_id = SELF_ID
        self.context_calls = 0
        self.sent = []
        self.directed = False
        self.hot = False
        self.sent_records = []

    async def build_chat_context(self, **kwargs):
        self.context_calls += 1
        return SimpleNamespace(
            group_id=str(GROUP_ID),
            directed_at_me=self.directed,
            max_history_messages=12,
            max_history_chars=2000,
        )

    async def send_message(self, event, message):
        self.sent.append(message)
        return {"status": "ok", "data": {"message_id": 7}}

    def remember_sent_message(self, event, text, result=None, **kwargs):
        self.sent_records.append(text)

    def thread_is_hot(self, thread_id, hot_seconds=None):
        return self.hot


class FakeAgent:
    def __init__(self, replies=("插一句",)):
        self.replies = list(replies)
        self.calls = []

    async def maybe_interject(self, context, thread_id="", reason=""):
        self.calls.append({"thread_id": thread_id, "reason": reason})
        return list(self.replies)


def _svc(mp, agent, **overrides) -> InterjectService:
    # 不读用户真实配置：用临时目录 + 显式 overrides
    config = _settings(**overrides)
    return InterjectService(mp=mp, config=config, agent_getter=lambda: agent)


def _params(**overrides) -> dict:
    base = {
        "enable": True,
        "frequent": 10,
        "extra": 5,
        "idle": 60.0,
        "cooldown": 0.0,
        "hot_minutes": 10.0,
        "hot_frequent": 1,
        "hot_idle": 1.0,
        "groups": [],
        "blacklist": [],
    }
    base.update(overrides)
    return base


# ── 触发路径 ────────────────────────────────────────────────
def test_triggers_after_enough_messages():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=3, extra_history=2, idle_minutes=60, cooldown_minutes=0)
        for seq in range(3):
            svc.note_message(_event(seq))
        await asyncio.sleep(0.05)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert len(agent.calls) == 1
    assert mp.sent == ["插一句"]
    assert agent.calls[0]["reason"] == "攒够条数"


def test_below_threshold_does_not_trigger():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=10, idle_minutes=60)
        for seq in range(4):
            svc.note_message(_event(seq))
        await asyncio.sleep(0.05)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert agent.calls == []
    assert mp.context_calls == 0


def test_idle_timer_triggers_even_with_few_messages():
    """群里只有两条新消息、凑不够 frequent，定时器到点也要看一眼。"""

    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=10)
        svc._params = lambda: _params(idle=0.05)
        svc.note_message(_event(1))
        svc.note_message(_event(2))
        await asyncio.sleep(0.2)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert len(agent.calls) == 1
    assert agent.calls[0]["reason"] == "安静了一阵"


def test_idle_timer_without_new_messages_just_recounts():
    """定时器到点但没有新消息 → 不判断，只进入下一轮倒计时。"""

    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=10)
        svc._params = lambda: _params(idle=0.05)
        svc.note_message(_event(1))          # 触发一次
        await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)             # 再来一轮：count 已经是 0
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert len(agent.calls) == 1


# ── 各种不该触发的情况 ──────────────────────────────────────
def test_cooldown_blocks_back_to_back():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=2, cooldown_minutes=5)
        svc.note_message(_event(1))
        svc.note_message(_event(2))
        await asyncio.sleep(0.05)
        svc.note_message(_event(3))
        svc.note_message(_event(4))
        await asyncio.sleep(0.05)
        svc.close()
        return agent

    assert len(asyncio.run(run()).calls) == 1


def test_hot_followup_during_cooldown_is_checked_after_cooldown():
    """Bot 刚回完话，对方在冷却期追问一句，冷却结束要补看一次。"""

    async def run():
        mp, agent = FakeMP(), FakeAgent()
        mp.hot = True
        svc = _svc(mp, agent, frequent=10, hot_frequent=3)
        svc._params = lambda: _params(
            frequent=10, hot_frequent=3, cooldown=0.05, idle=60.0
        )
        svc.note_message(_event(0))       # 建 state
        svc.note_bot_spoke(GROUP_ID)      # 模拟 Bot 刚回复，进冷却
        svc.note_message(_event(1))       # 敏感期单条追问
        await asyncio.sleep(0.15)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert len(agent.calls) == 1
    assert agent.calls[0]["reason"] == "冷却结束"
    assert mp.sent == ["插一句"]


def test_threshold_during_cooldown_is_not_dropped():
    """冷却期间攒够条数，不能直接清零；冷却结束要补判断。"""

    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=2)
        svc._params = lambda: _params(frequent=2, cooldown=0.05, idle=60.0)
        svc.note_message(_event(0))       # 建 state
        svc.note_bot_spoke(GROUP_ID)      # 进冷却
        svc.note_message(_event(1))
        svc.note_message(_event(2))       # 达到 frequent
        await asyncio.sleep(0.15)
        svc.close()
        return agent

    agent = asyncio.run(run())
    assert len(agent.calls) == 1
    assert agent.calls[0]["reason"] == "冷却结束"


def test_directed_message_is_left_to_normal_chat():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        mp.directed = True
        svc = _svc(mp, agent, frequent=2, cooldown_minutes=0)
        svc.note_message(_event(1))
        svc.note_message(_event(2))
        await asyncio.sleep(0.05)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert agent.calls == []
    assert mp.sent == []


def test_bot_spoke_resets_counter():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=3, cooldown_minutes=0)
        svc.note_message(_event(1))
        svc.note_message(_event(2))
        svc.note_bot_spoke(GROUP_ID)
        svc.note_message(_event(3))
        await asyncio.sleep(0.05)
        svc.close()
        return agent

    assert asyncio.run(run()).calls == []


def test_group_whitelist_and_blacklist():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=1, groups=["999"], cooldown_minutes=0)
        assert svc.note_message(_event(1)) is None       # 不在白名单
        svc2 = _svc(mp, agent, frequent=1, blacklist_groups=[str(GROUP_ID)], cooldown_minutes=0)
        assert svc2.note_message(_event(2)) is None      # 黑名单
        svc.close()
        svc2.close()
        return agent

    assert asyncio.run(run()).calls == []


def test_own_messages_are_not_counted():
    async def run():
        mp, agent = FakeMP(), FakeAgent()
        svc = _svc(mp, agent, frequent=1, cooldown_minutes=0)
        assert svc.note_message(_event(1, user_id=SELF_ID)) is None
        svc.close()
        return agent

    assert asyncio.run(run()).calls == []


def test_agent_not_ready_is_skipped():
    async def run():
        mp = FakeMP()
        svc = InterjectService(
            mp=mp,
            config=_settings(),
            agent_getter=lambda: None,
        )
        svc._params = lambda: _params(frequent=1, cooldown=0)
        svc.note_message(_event(1))
        await asyncio.sleep(0.05)
        svc.close()
        return mp

    assert asyncio.run(run()).sent == []


# ── 「先判断再开口」 ────────────────────────────────────────
def test_parse_interject_judge():
    assert _parse_interject_judge("<judge>yes</judge>") is True
    assert _parse_interject_judge("<JUDGE> YES </JUDGE>") is True
    assert _parse_interject_judge("<judge>no</judge>") is False
    assert _parse_interject_judge("yes，可以插一句") is True
    assert _parse_interject_judge("no") is False
    assert _parse_interject_judge("") is False


class _StubLLM:
    def __init__(self, verdict):
        self.verdict = verdict

    async def ainvoke(self, messages):
        return SimpleNamespace(content=self.verdict)


def _fake_agent_with_judge(verdict, replies=("插一句",)):
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.llm = _StubLLM(verdict)
    calls = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return list(replies)

    agent.chat = fake_chat
    return agent, calls


def test_maybe_interject_skips_when_judge_says_no():
    agent, calls = _fake_agent_with_judge("<judge>no</judge>")
    ctx = SimpleNamespace(group_id=str(GROUP_ID), render=lambda: "x")
    assert asyncio.run(agent.maybe_interject(ctx)) is None
    assert calls == []          # 判断不通过 → 不再请求模型


def test_maybe_interject_speaks_when_judge_says_yes():
    agent, calls = _fake_agent_with_judge("<judge>yes</judge>")
    ctx = SimpleNamespace(group_id=str(GROUP_ID), render=lambda: "x")
    plan = asyncio.run(agent.maybe_interject(ctx, thread_id="group_1"))
    # 旧格式（yes/no）降级成 join
    assert plan.act == "join"
    assert plan.messages == ["插一句"]
    assert len(calls) == 1
    # 插话时不要再注入「动作检查」，而且要带上 <interject> 说明
    assert calls[0]["action_check"] is False
    assert calls[0]["hint"] == _INTERJECT_REPLY_HINT


def test_maybe_interject_answer_act_uses_answer_hint():
    """答悬空提问和随口接一句，生成指令不是同一段。"""
    agent, calls = _fake_agent_with_judge(
        "<verdict><act>answer</act><why>没人答</why><reply_to>1059815600</reply_to></verdict>"
    )
    ctx = SimpleNamespace(group_id=str(GROUP_ID), render=lambda: "x")
    plan = asyncio.run(agent.maybe_interject(ctx, thread_id="group_1"))
    assert plan.act == "answer"
    assert plan.reply_to == "1059815600"
    assert plan.why == "没人答"
    assert calls[0]["hint"] == interject_hint("answer")
    assert calls[0]["hint"] != _INTERJECT_REPLY_HINT


def test_maybe_interject_nod_does_not_call_model():
    """nod 的语义就是「只贴表情不打字」，不该再花一次生成。"""
    agent, calls = _fake_agent_with_judge("<verdict><act>nod</act><why>在场</why></verdict>")
    ctx = SimpleNamespace(group_id=str(GROUP_ID), render=lambda: "x")
    plan = asyncio.run(agent.maybe_interject(ctx, thread_id="group_1"))
    assert plan.act == "nod"
    assert plan.messages == []
    assert calls == []


def test_hot_window_lowers_threshold_and_records_sent():
    """敏感期里 2 条就够（普通要 10 条），说完还要记进临时历史。"""

    async def run():
        mp, agent = FakeMP(), FakeAgent()
        mp.hot = True
        svc = _svc(
            mp, agent,
            frequent=10, hot_frequent=2, hot_minutes=10, cooldown_minutes=0,
        )
        svc.note_message(_event(1))
        svc.note_message(_event(2))
        await asyncio.sleep(0.05)
        svc.close()
        return mp, agent

    mp, agent = asyncio.run(run())
    assert len(agent.calls) == 1
    assert mp.sent_records == ["插一句"]
