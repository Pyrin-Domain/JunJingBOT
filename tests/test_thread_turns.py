"""多轮留存的纯函数部分：``ThreadTurns`` 与 ``render_increment``。

目标是让 prompt 前缀稳定：第一轮的完整上下文冻结，之后每轮只追加
``<at>`` + ``<current>``。所以这里要钉住两件事：

1. 增量块里**不能**出现 ``<scene>`` / ``<history>``（一出现前缀就变）；
2. 留存到上限时**整段清空**，不做滑动窗口（滑窗会让人以为省了 token，
   实际上每轮前缀都变，缓存一直 miss）。
"""

from __future__ import annotations

from datetime import datetime

from agent.runtime import QQBotAgent
from agent.thread_turns import ThreadTurns
from messaging.models import ChatContext, ChatMessage, Segment, Sender


def _context(text: str = "看看这个", *, history: int = 3) -> ChatContext:
    now = datetime(2026, 11, 13, 23, 40)
    history_messages = [
        ChatMessage(
            message_id=str(1000 + index),
            time=datetime(2026, 11, 13, 23, index),
            sender=Sender(qq="10001", name=f"群友{index}"),
            segments=[Segment("text", text=f"历史{index}")],
        )
        for index in range(history)
    ]
    return ChatContext(
        chat_type="group",
        group_id="20001",
        group_name="测试群",
        self_id="10002",
        self_name="君景",
        master_ids=["10001"],
        now=now,
        history=history_messages,
        current=ChatMessage(
            message_id="1059815617",
            time=now,
            sender=Sender(qq="10001", name="Pyrin", is_master=True),
            segments=[Segment("text", text=text)],
        ),
    )


# ══════════════════════════════════════════════════════════
# 增量渲染
# ══════════════════════════════════════════════════════════
def test_render_increment_has_no_scene_or_history():
    text = _context().render_increment()

    assert "<at>" in text
    assert "<current>" in text
    assert "看看这个" in text
    assert "<scene>" not in text
    assert "<history>" not in text


def test_render_increment_is_stable_for_same_moment():
    context = _context()

    assert context.render_increment() == context.render_increment()


def test_increment_includes_missed_messages_since():
    """多轮模式下，Bot 没参与的那几条群聊不能丢（放进 <since>）。"""
    text = _context().render_increment(since="1000")

    assert "<since>" in text
    assert "历史1" in text and "历史2" in text
    assert "历史0" not in text


def test_increment_since_outside_window_keeps_whole_history():
    """上一轮那条滚出窗口时，宁可把窗口里现有的都带上，也别丢中间的。"""
    text = _context().render_increment(since="999999")

    assert "历史0" in text and "历史1" in text and "历史2" in text


def test_increment_without_since_has_no_since_block():
    text = _context().render_increment()

    assert "<since>" not in text
    assert "<current>" in text


def test_full_render_still_contains_scene_and_history():
    """老路径不能被改坏：render() 仍然带 scene / history。"""
    text = _context().render()

    assert "<scene>" in text
    assert "<history>" in text
    assert "<current>" in text


# ══════════════════════════════════════════════════════════
# ThreadTurns
# ══════════════════════════════════════════════════════════
def test_start_then_append_keeps_order():
    turns = ThreadTurns()

    turns.start("g1", "H1", "A1")
    assert turns.append("g1", "H2", "A2") is True

    got = turns.get("g1")
    assert [(t.human, t.ai) for t in got] == [("H1", "A1"), ("H2", "A2")]
    assert turns.stats("g1")["chars"] == len("H1A1H2A2")


def test_append_without_start_returns_false():
    turns = ThreadTurns()

    assert turns.append("g1", "H2", "A2") is False
    assert turns.get("g1") == []


def test_overflow_clears_whole_thread():
    """超过 max_turns 要整段清空，不能只丢最旧的一轮（滑窗会让前缀每轮都变）。"""
    turns = ThreadTurns(max_turns=2)
    turns.start("g1", "H1", "A1")
    turns.append("g1", "H2", "A2")

    alive = turns.append("g1", "H3", "A3")

    assert alive is False
    assert turns.get("g1") == []


def test_char_budget_also_clears():
    turns = ThreadTurns(max_turns=10, max_chars=1000)
    turns.start("g1", "H" * 400, "A" * 400)

    assert turns.append("g1", "H" * 400, "A") is False
    assert turns.get("g1") == []


def test_expired_thread_is_dropped():
    turns = ThreadTurns(ttl=-1)
    turns.start("g1", "H1", "A1")

    assert turns.get("g1") == []
    assert turns.started("g1") is False


def test_duplicate_append_is_ignored():
    """并发下同一条消息可能被追加两次（按 token 判重）。"""
    turns = ThreadTurns()
    turns.start("g1", "H1", "A1", token="m1")

    assert turns.append("g1", "H2", "A2", token="m2") is True
    assert turns.append("g1", "H2", "A2", token="m2") is True
    assert len(turns.get("g1")) == 2


def test_same_text_different_token_is_kept():
    """连发两句一模一样的话不能被当成重复吞掉。"""
    turns = ThreadTurns()
    turns.start("g1", "在吗", "嗯", token="m1")

    assert turns.append("g1", "在吗", "嗯", token="m2") is True
    assert len(turns.get("g1")) == 2


def test_reset_and_clear():
    turns = ThreadTurns()
    turns.start("g1", "H1", "A1")
    turns.start("g2", "H1", "A1")

    turns.reset("g1")
    assert turns.get("g1") == []
    assert turns.started("g2") is True

    turns.clear()
    assert turns.started("g2") is False


# ══════════════════════════════════════════════════════════
# 对照实验：奇数日 / 偶数日
# ══════════════════════════════════════════════════════════
def _arm_agent(parity):
    from agent.thread_turns import ThreadTurns as _Turns

    agent = QQBotAgent.__new__(QQBotAgent)
    agent.thread_turns = _Turns()
    agent._thread_memory_enabled = True
    agent._thread_memory_verdict = True
    agent._thread_memory_new_after = 0
    agent._thread_memory_groups = set()
    agent._thread_memory_day_parity = parity
    return agent


def test_arm_follows_day_parity():
    from datetime import date

    agent = _arm_agent(1)  # 奇数日开
    on, why = agent._memory_arm("group_1", today=date(2026, 11, 15))
    off, why_off = agent._memory_arm("group_1", today=date(2026, 11, 16))

    assert on is True and "奇数日" in why
    assert off is False and "偶数日" in why_off

    flipped = _arm_agent(0)  # 偶数日开
    assert flipped._memory_arm("group_1", today=date(2026, 11, 15))[0] is False
    assert flipped._memory_arm("group_1", today=date(2026, 11, 16))[0] is True


def test_arm_without_experiment_is_always_on():
    agent = _arm_agent(None)
    assert agent._memory_arm("group_1") == (True, "on")


def test_arm_respects_group_whitelist_before_experiment():
    """灰度群和对照实验是「与」关系：不在名单里的群永远不生效。"""
    agent = _arm_agent(1)
    agent._thread_memory_groups = {"20001"}
    assert agent._memory_arm("group_20001")[0] is True
    assert agent._memory_arm("group_99999") == (False, "not-in-groups")


def test_arm_off_when_switch_disabled():
    agent = _arm_agent(1)
    agent._thread_memory_enabled = False
    assert agent._memory_arm("group_1") == (False, "disabled")
