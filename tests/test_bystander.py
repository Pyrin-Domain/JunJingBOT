"""插话的旁听视角：局面（pulse）、自我位置（me）、兴趣命中、动作判断。

这些是「重新设计插话上下文」的核心：不再是「用回复的上下文 + 一道是/否题」，
而是「几个人在聊、我刚说过什么、有没有人提问没人接」+「这一刻缺什么角色」。
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from agent.output import InterjectPlan, parse_interject_verdict
from bot.services.interject import InterjectService
from messaging.interest import read_interests
from messaging.models import ChatContext, ChatMessage, Segment, Sender
from messaging.pulse import read_pulse, read_self_position

NOW = datetime(2026, 10, 24, 21, 0)
SELF = "10002"
MASTER = "10001"
FRIEND = "1341538478"

NAMES = {SELF: "君景", MASTER: "馆馆", FRIEND: "地板"}


def _msg(seq: int, qq: str, text: str, *, minutes_ago: float = 0, is_self: bool = False):
    return ChatMessage(
        message_id=str(seq),
        time=NOW - timedelta(minutes=minutes_ago),
        sender=Sender(qq=qq, name=NAMES.get(qq, qq), is_self=is_self),
        segments=[Segment("text", text=text)],
    )


# ── 局面 ────────────────────────────────────────────────
def test_pulse_detects_two_person_dialogue():
    messages = [
        _msg(1, MASTER, "在吗", minutes_ago=8),
        _msg(2, FRIEND, "在", minutes_ago=7),
        _msg(3, MASTER, "今天吃什么", minutes_ago=6),
        _msg(4, FRIEND, "随便", minutes_ago=5),
    ]
    pulse = read_pulse(messages, now=NOW, self_id=SELF)
    assert pulse.speakers == 2
    assert pulse.dialogue is True
    assert pulse.messages == 4


def test_pulse_detects_ending():
    messages = [_msg(1, MASTER, "聊点别的"), _msg(2, FRIEND, "那我去睡了")]
    assert read_pulse(messages, now=NOW).ended is True


def test_pulse_detects_dangling_question():
    """最后一条是提问、没人接 → 这是插话价值最高的时刻。"""
    messages = [
        _msg(1, MASTER, "晚上吃啥", minutes_ago=10),
        _msg(2, FRIEND, "不知道", minutes_ago=8),
        _msg(3, MASTER, "有人知道教务系统怎么登吗", minutes_ago=2),
    ]
    pulse = read_pulse(messages, now=NOW)
    assert pulse.has_question
    assert pulse.question_who == "馆馆"
    assert pulse.question_id == "3"


def test_pulse_question_answered_is_not_dangling():
    messages = [
        _msg(1, MASTER, "有人知道教务系统怎么登吗", minutes_ago=10),
        _msg(2, FRIEND, "用统一认证", minutes_ago=9),
    ]
    assert read_pulse(messages, now=NOW).has_question is False


def test_pulse_ignores_own_messages_when_counting_speakers():
    messages = [
        _msg(1, MASTER, "在吗"),
        _msg(2, SELF, "在的", is_self=True),
        _msg(3, FRIEND, "哈哈"),
    ]
    assert read_pulse(messages, now=NOW).speakers == 2


# ── 自我位置 ────────────────────────────────────────────
def test_self_position_warns_about_just_spoke():
    messages = [
        _msg(1, MASTER, "在吗", minutes_ago=10),
        _msg(2, SELF, "在群里挂着水群呢", minutes_ago=2, is_self=True),
    ]
    position = read_self_position(messages, now=NOW)
    assert position.spoke_just_now is True
    assert position.last_text == "在群里挂着水群呢"
    rendered = "\n".join(position.render())
    assert "刚说完" in rendered


def test_self_position_when_never_spoke():
    messages = [_msg(1, MASTER, "在吗")]
    position = read_self_position(messages, now=NOW)
    assert position.last_minutes is None
    assert "还没在这个群说过话" in "\n".join(position.render())


# ── 兴趣 ────────────────────────────────────────────────
def test_interest_hit_and_weight():
    messages = [
        _msg(1, MASTER, "我又买了条小裙子"),
        _msg(2, FRIEND, "作业写完了吗"),
    ]
    hits = read_interests(messages, high=["小裙子"], low=["作业"])
    topics = {hit.topic: hit.weight for hit in hits}
    assert topics == {"作业": "low", "小裙子": "high"}
    # 越近的越靠前
    assert hits[0].topic == "作业"


def test_interest_ignores_self_and_limit():
    messages = [
        _msg(1, MASTER, "女装好看", minutes_ago=3),
        _msg(2, SELF, "女装我也喜欢", minutes_ago=2, is_self=True),
        _msg(3, FRIEND, "可爱", minutes_ago=1),
    ]
    hits = read_interests(messages, high=["女装", "可爱"], limit=1)
    assert len(hits) == 1
    assert hits[0].topic == "可爱"


# ── 旁听视角渲染 ────────────────────────────────────────
def _context(messages) -> ChatContext:
    return ChatContext(
        chat_type="group",
        group_id="20001",
        self_id=SELF,
        now=NOW,
        history=messages[:-1],
        current=messages[-1],
    )


def test_bystander_uses_tail_not_current():
    """<tail> 只是「最后一条」，不该继承 <current> 那种「必须回」的语义。"""
    text = _context([_msg(1, MASTER, "今天吃什么")]).render_as_bystander()
    assert "<tail>" in text
    assert "<current>" not in text


def test_bystander_includes_me_and_pulse():
    ctx = _context(
        [
            _msg(1, SELF, "在群里挂着水群呢", minutes_ago=5, is_self=True),
            _msg(2, MASTER, "有人知道教务系统怎么登吗", minutes_ago=1),
        ]
    )
    text = ctx.render_as_bystander()
    assert "<me>" in text
    assert "<pulse>" in text
    assert "悬空提问" in text


def test_bystander_includes_interest_hits():
    ctx = _context([_msg(1, MASTER, "我又买了条小裙子")])
    ctx.interest_hits = read_interests([ctx.current], high=["小裙子"])
    assert "<interest>" in ctx.render_as_bystander()


# ── 动作判断解析 ────────────────────────────────────────
def test_verdict_parses_act():
    plan = parse_interject_verdict(
        "<verdict><act>answer</act><why>问了个没人答的问题</why>"
        "<reply_to>1059815600</reply_to></verdict>"
    )
    assert plan.act == "answer"
    assert plan.why == "问了个没人答的问题"
    assert plan.reply_to == "1059815600"


def test_verdict_falls_back_for_legacy_yes_no():
    assert parse_interject_verdict("<judge>yes</judge>").act == "join"
    assert parse_interject_verdict("<judge>no</judge>").act == "silent"


def test_verdict_garbage_is_silent():
    assert parse_interject_verdict("嗯……").act == "silent"
    assert parse_interject_verdict("").act == "silent"
    assert parse_interject_verdict("<act>bogus</act>").act == "silent"


def test_verdict_drops_copied_placeholder_reason():
    """模型把提示词里的占位说明抄进 <why>（日志里那句「一句话理由」）。

    理由要如实留空，不能把占位文字当成判断依据记进日志。
    """
    plan = parse_interject_verdict(
        "<verdict><act>silent</act><why>一句话理由</why></verdict>"
    )
    assert plan.act == "silent"
    assert plan.why == ""


def test_verdict_keeps_real_reason():
    plan = parse_interject_verdict(
        "<verdict><act>nod</act><why>他们在聊，我没内容但想露个面</why></verdict>"
    )
    assert plan.why == "他们在聊，我没内容但想露个面"


# ── nod：只说「我在」，不打字 ─────────────────────────────
class _FakeAPI:
    def __init__(self):
        self.calls = []

    async def set_msg_emoji_like(self, message_id, emoji_id):
        self.calls.append((str(message_id), str(emoji_id)))
        return {"status": "ok"}


class _NodMP:
    def __init__(self):
        self.user_id = SELF
        self.api = _FakeAPI()
        self.sent = []

    async def build_chat_context(self, **kwargs):
        return SimpleNamespace(
            group_id="20001",
            directed_at_me=False,
            history=[],
            current=None,
            max_history_messages=12,
            max_history_chars=2000,
        )

    async def send_message(self, event, message):
        self.sent.append(message)
        return {"status": "ok", "data": {"message_id": 7}}

    def remember_sent_message(self, event, text, result=None, **kwargs):
        return None

    def thread_is_hot(self, thread_id, hot_seconds=None):
        return False


class _NodAgent:
    def __init__(self, plan):
        self.plan = plan

    async def maybe_interject(self, context, thread_id="", reason=""):
        return self.plan


def _nod_event() -> dict:
    return {
        "message_type": "group",
        "group_id": 20001,
        "user_id": MASTER,
        "message_id": 555,
        "message_seq": 555,
    }


def _run_plan(plan):
    mp = _NodMP()
    service = InterjectService(
        mp=mp, settings={"interject": {"enable": True}}, agent_getter=lambda: _NodAgent(plan)
    )
    state = service._state("20001")
    state.last_event = _nod_event()
    asyncio.run(service._run(state, "测试"))
    return mp, state


def test_nod_only_reacts_without_speaking():
    mp, state = _run_plan(InterjectPlan(act="nod"))
    assert mp.sent == []                       # 没有打字
    assert len(mp.api.calls) == 1              # 但贴了表情
    assert mp.api.calls[0][0] == "555"
    assert state.nodded == 1
    assert state.last_act == "nod"


def test_join_speaks_and_records_history():
    mp, state = _run_plan(InterjectPlan(act="join", messages=["接一句"]))
    assert mp.sent == ["接一句"]
    assert mp.api.calls == []
    assert state.triggered == 1
