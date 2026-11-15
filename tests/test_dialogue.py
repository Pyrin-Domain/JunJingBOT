"""对话关系：每条消息是「谁对谁说的」+ 主回复的 ``<dialogue>`` 摘要。

覆盖的真问题：
- 群里 A 接 B 的话（没 @ 没引用）被模型当成「冲自己说的」，硬接一句；
- 引用的原消息取不到时，``↳`` 整段消失，关系凭空断掉；
- 正常回复时模型看不到「这段是两个人在聊，我是旁听」。
"""

from datetime import datetime, timedelta

from messaging.dialogue import (
    TURN_GAP_SECONDS,
    addressee_markers,
    describe_dialogue,
    resolve_addressees,
)
from messaging.models import (
    ChatContext,
    ChatMessage,
    Quote,
    Segment,
    Sender,
    render_message,
)

NOW = datetime(2026, 11, 10, 20, 28)
SELF = "3122793026"
MASTER = "1013098110"
GROUP = "1129281879"


def _msg(mid, qq, name, text, *, minutes_ago=0, quote=None, is_self=False, segments=None):
    return ChatMessage(
        message_id=mid,
        time=NOW - timedelta(minutes=minutes_ago),
        sender=Sender(qq=qq, name=name, is_self=is_self),
        segments=segments if segments is not None else [Segment("text", text=text)],
        quote=quote,
    )


def _quote(mid, qq, name, summary):
    return Quote(message_id=mid, sender=Sender(qq=qq, name=name), summary=summary)


def _ctx(history, current, **kwargs):
    return ChatContext(
        chat_type="group",
        group_id=GROUP,
        group_name="蝎子莱莱",
        self_id=SELF,
        self_name="君景",
        master_ids=[MASTER],
        now=NOW,
        history=history,
        current=current,
        aliases=("君景", "独角兽"),
        **kwargs,
    )


# ── 指向推断 ────────────────────────────────────────────────
def test_addressee_falls_back_to_previous_speaker():
    """既没 @ 也没引用 → 就是在接上一个人的话。"""
    msgs = [
        _msg("1", "20002", "羽鹤", "我们线下face to face", minutes_ago=1),
        _msg("2", "20003", "喻家康成", "不是dick to dick？"),
    ]
    marks = addressee_markers(msgs, self_id=SELF)
    assert "1" not in marks          # 第一条前面没人，只能算对群里说
    assert marks["2"] == "→羽鹤"


def test_addressee_at_is_not_marked_because_body_shows_it():
    msgs = [
        ChatMessage(
            message_id="1",
            time=NOW,
            sender=Sender(qq="20002", name="羽鹤"),
            segments=[Segment("at", qq="20003"), Segment("text", text=" 带我打")],
        )
    ]
    assert addressee_markers(msgs, {"20003": "陌城"}, self_id=SELF) == {}


def test_addressee_reply_points_at_quoted_sender():
    msgs = [
        _msg(
            "1",
            "20002",
            "羽鹤",
            "为什么撤回",
            quote=_quote("900", SELF, "君景", "我撤回了"),
        )
    ]
    addressees = resolve_addressees(msgs, self_id=SELF)
    assert addressees[0].kind == "reply"
    assert addressees[0].label == "我"


def test_addressee_name_call_marks_me():
    """没 @ 但直呼其名 —— 这句是对 Bot 说的。"""
    msgs = [_msg("1", "20002", "羽鹤", "君景在吗")]
    assert addressee_markers(msgs, self_id=SELF, aliases=("君景",))["1"] == "→我"


def test_addressee_ignores_stale_turn():
    """间隔太久就不是接话，不该硬说「他在接谁」。"""
    msgs = [
        _msg("1", "20002", "羽鹤", "晚安", minutes_ago=60),
        _msg("2", "20003", "喻家康成", "今天吃啥"),
    ]
    addressees = resolve_addressees(msgs, self_id=SELF)
    assert addressees[1].kind == "group"
    assert addressees[1].label == ""


def test_addressee_same_sender_keeps_looking_back():
    """自己连发两条：第二条不是「接自己的话」，要找回上一个人。"""
    msgs = [
        _msg("1", "20002", "羽鹤", "打不打", minutes_ago=1),
        _msg("2", SELF, "君景", "不打", minutes_ago=1, is_self=True),
        _msg("3", SELF, "君景", "我要睡了", is_self=True),
    ]
    addressees = resolve_addressees(msgs, self_id=SELF)
    assert addressees[2].label == "羽鹤"


def test_turn_gap_constant_is_sane():
    assert 60 <= TURN_GAP_SECONDS <= 600


# ── 渲染 ────────────────────────────────────────────────────
def test_render_message_appends_addressee_marker():
    line = render_message(
        _msg("1", "20002", "羽鹤", "打不打"), now=NOW, addressee="→陌城"
    )
    assert line.endswith("：打不打　→陌城")


def test_quote_missing_still_shows_the_relation():
    """引用的原消息取不到：不能整段消失，得如实说「已取不到」。"""
    msg = _msg("9", MASTER, "Pyrin", "为什么撤回")
    msg.reply_to_id = "888"
    line = render_message(msg, now=NOW)
    assert "↳[888] 原消息已取不到" in line


def test_history_and_current_both_carry_markers():
    history = [_msg("1", "20002", "羽鹤", "我们线下face to face", minutes_ago=1)]
    current = _msg("2", "20003", "喻家康成", "不是dick to dick？")
    text = _ctx(history, current).render()
    assert "→羽鹤" in text
    assert text.count("→羽鹤") >= 1


def test_self_message_does_not_get_marker():
    """自己说过的话由 <dialogue> 负责，行尾不重复标。"""
    history = [
        _msg("1", "20002", "羽鹤", "打不打", minutes_ago=1),
        _msg("2", SELF, "君景", "不打", is_self=True),
    ]
    text = _ctx(history, _msg("3", "20002", "羽鹤", "真不打？")).render()
    assert "：不打　→" not in text


# ── <dialogue> ─────────────────────────────────────────────
def test_dialogue_says_i_am_only_listening():
    history = [
        _msg("1", "20002", "羽鹤", "我们线下face to face", minutes_ago=2),
        _msg("2", MASTER, "Pyrin", "飞羽 在 开专 还是 凌水", minutes_ago=1),
    ]
    text = _ctx(history, _msg("3", "20003", "喻家康成", "不是dick to dick？")).render()
    assert "<dialogue>" in text
    assert "没有点你就别抢话" in text
    assert "最后一条：喻家康成 接着 Pyrin 的话" in text


def test_dialogue_marks_message_aimed_at_me():
    history = [_msg("1", "20002", "羽鹤", "在吗", minutes_ago=1)]
    current = ChatMessage(
        message_id="2",
        time=NOW,
        sender=Sender(qq="20002", name="羽鹤"),
        segments=[Segment("at", qq=SELF), Segment("text", text=" 出来")],
    )
    text = _ctx(history, current).render()
    assert "对你说的" in text
    assert "没有点你就别抢话" not in text


def test_dialogue_is_skipped_for_very_short_exchanges():
    """只有一两句时不给局面块，省 token。"""
    text = _ctx([], _msg("1", "20002", "羽鹤", "在吗")).render()
    assert "<dialogue>" not in text


def test_dialogue_counts_include_self():
    view = describe_dialogue(
        [
            _msg("1", "20002", "羽鹤", "a", minutes_ago=3),
            _msg("2", SELF, "君景", "b", minutes_ago=2, is_self=True),
            _msg("3", "20002", "羽鹤", "c", minutes_ago=1),
        ],
        self_id=SELF,
    )
    assert view.self_count == 1
    assert view.counts == [("羽鹤", 2)]


# ── 引用被 Bot 的消息（摘要缺失时的兜底）─────────────────
def test_replies_to_me_survives_missing_quote():
    ctx = _ctx([], _msg("1", "20002", "羽鹤", "接着说"))
    ctx.current.reply_to_id = "777"
    ctx.current.reply_to_self = True
    assert ctx.replies_to_me is True


def test_replies_to_me_from_quote_sender():
    ctx = _ctx(
        [],
        _msg("1", "20002", "羽鹤", "接着说", quote=_quote("777", SELF, "君景", "原话")),
    )
    assert ctx.replies_to_me is True


# ── 采集层：引用 ID 必须留住 ────────────────────────────────
def test_collect_message_keeps_reply_id_when_quote_missing():
    import asyncio

    from messaging.processor import MessageProcessor

    class _MP(MessageProcessor):
        async def ensure_connected(self):
            return None

        async def _get_message_event(self, message_id, cache=None):
            return None

    event = {
        "message_id": "500",
        "time": NOW.timestamp(),
        "user_id": "20002",
        "sender": {"user_id": "20002", "nickname": "羽鹤"},
        "message": [
            {"type": "reply", "data": {"id": "499"}},
            {"type": "text", "data": {"text": "你刚才说什么"}},
        ],
    }

    async def run():
        mp = _MP(None, None)
        mp.user_id = SELF
        return await mp.collect_message(event, master_ids=[MASTER], now=NOW)

    msg = asyncio.run(run())
    assert msg.quote is None
    assert msg.reply_to_id == "499"
    assert "原消息已取不到" in render_message(msg, now=NOW)
