"""process_message 的处理顺序：被 @ 时 AI 优先，调用模型后不再走关键词。

否则同一条消息会被 AI 和 keyword_actions 各回一次（双发）。
"""
import asyncio
import threading

from bot.pipeline import MessagePipeline


class _FakeContext:
    def render_history_only(self):
        return ""


class _FakeMP:
    def __init__(self):
        self.user_id = 10002
        self.check_point = {}
        self.extraconfig = {"recent": False}
        self.ated = True
        self.keyword_calls = 0
        self.sent = []
        self.context_calls = 0
        self.remembered = []
        self.sent_records = []
        self.hot = False
        self.reply_to_bot = False

    async def is_AT(self, event, user_id=None):
        return self.user_id if self.ated else None

    def get_clean_context(self, event, *args, **kwargs):
        return event.get("raw_message", "")

    async def build_chat_context(self, **kwargs):
        self.context_calls += 1
        return _FakeContext()

    # ── 临时会话历史 ──
    @staticmethod
    def thread_id_of(event):
        return f"group_{event.get('group_id')}"

    def remember_thread_message(self, event, **kwargs):
        self.remembered.append(event.get("message_id"))

    def remember_sent_message(self, event, text, result=None, **kwargs):
        self.sent_records.append(text)

    def thread_is_hot(self, thread_id, hot_seconds=None):
        return self.hot

    def replies_to_bot(self, event):
        return self.reply_to_bot

    async def generate_structed_message_to_creat_context(self, event):
        return "主人：看看女装"

    async def pattern_match(self, event=None, pattern_config=None):
        self.keyword_calls += 1
        return False

    async def send_message(self, event=None, message=None):
        self.sent.append(message)
        return {"status": "ok", "data": {"message_id": 1}}

    async def ensure_connected(self):
        return None

    async def get_history_msg(self, *args, **kwargs):
        return None


class _FakeForward:
    async def auto_forward(self, **kwargs):
        return False


class _FakeConfig:
    auto_forward_enabled = False
    agent_enabled = True
    master_qq = ["10001"]

    def get(self, key_path, default=None):
        return default

    def prompt(self, key):
        return {
            "history_command_regex": r"^历史",
            "group_context": "群{group_id} 用户{user_id} 消息{message_id} at{ated_user} self{self_id} master{master_id}",
            "private_context": "私聊{user_id}",
            "history_context": "历史: {history}",
        }[key]

    def reply(self, key):
        return f"[回复]{key}"


class _FakeAgent:
    def __init__(self, reply):
        self.reply = reply
        self.chat_calls = 0
        self.last_kwargs = {}

    async def chat(self, **kwargs):
        self.chat_calls += 1
        self.last_kwargs = kwargs
        return self.reply


class _FakeInterject:
    """插话服务：只记录被喂了哪些消息，以及被判定成了什么注意力。"""

    def __init__(self):
        self.notes = []
        self.spoke = []
        self.attentions = []

    def note_message(self, event, *, attention=None):
        self.notes.append(event.get("message_id"))
        self.attentions.append(getattr(attention, "level", None))
        return len(self.notes)

    def note_bot_spoke(self, group_id):
        self.spoke.append(str(group_id))


GROUP_EVENT = {
    "message_type": "group",
    "group_id": 20001,
    "user_id": 10001,
    "message_id": 90001,
    "message_seq": 90001,
    "raw_message": "看看女装",
}


def _bot(agent, ated=True):
    mp = _FakeMP()
    mp.ated = ated

    async def get_agent():
        return agent

    pipeline = MessagePipeline(
        mp=mp,
        settings=_FakeConfig(),
        forward=_FakeForward(),
        interject=_FakeInterject(),
        agent_getter=get_agent,
        keyword_builder=lambda: [],
    )
    pipeline.mp.ated = ated
    return pipeline


def test_ated_message_goes_to_ai_and_skips_keywords():
    agent = _FakeAgent(["喵，给你看看。"])
    bot = _bot(agent)
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 1
    assert bot.mp.keyword_calls == 0  # 调用了模型 → 不再做 pattern_match
    assert bot.mp.sent == ["喵，给你看看。"]
    # 上下文以结构化对象传入，不再拼 user_message 字符串
    assert bot.mp.context_calls == 1
    assert agent.last_kwargs.get("context") is not None
    assert "user_message" not in agent.last_kwargs


def test_ated_message_with_silent_agent_still_skips_keywords():
    agent = _FakeAgent([])  # 静默：模型调用了，只是不说话
    bot = _bot(agent)
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 1
    assert bot.mp.keyword_calls == 0
    assert bot.mp.sent == []


def test_plain_message_falls_back_to_keywords():
    agent = _FakeAgent(["不该出现"])
    bot = _bot(agent, ated=False)
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 0
    assert bot.mp.keyword_calls == 1


def test_ated_message_without_agent_falls_back_to_keywords():
    bot = _bot(agent=None)  # agent.enable=false
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert bot.mp.keyword_calls == 1


def test_history_command_still_shortcuts_before_ai():
    agent = _FakeAgent(["不该出现"])
    bot = _bot(agent)
    event = dict(GROUP_EVENT, raw_message="历史")
    bot.history_solution = lambda ev: asyncio.sleep(0, result=bot.mp.sent.append("[历史]"))
    asyncio.run(bot.process_message(event))
    assert agent.chat_calls == 0
    assert bot.mp.sent == ["[历史]"]


def test_group_message_is_counted_for_interjection():
    bot = _bot(_FakeAgent([]), ated=False)
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert bot.interject.notes == [GROUP_EVENT["message_id"]]


def test_recover_messages_are_not_counted():
    """断点恢复是在补掉线期间的历史，不能喂给插话。"""
    bot = _bot(_FakeAgent([]), ated=False)
    asyncio.run(bot.process_message(dict(GROUP_EVENT), is_recover=True))
    assert bot.interject.notes == []


def test_bot_spoke_resets_interject_counter():
    bot = _bot(_FakeAgent(["喵"]))
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert bot.interject.spoke == [str(GROUP_EVENT["group_id"])]


def test_reply_to_bot_in_hot_window_goes_to_ai():
    """Bot 刚说完话，对方回复它（没 @）→ 直接交给 AI 续聊。"""
    agent = _FakeAgent(["嗯，接着说。"])
    bot = _bot(agent, ated=False)
    bot.mp.hot = True
    bot.mp.reply_to_bot = True
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 1
    assert bot.mp.sent == ["嗯，接着说。"]
    # 发出去的话要记进临时历史，才能算「对方回的是哪句」
    assert bot.mp.sent_records == ["嗯，接着说。"]


def test_reply_to_bot_outside_hot_window_falls_back_to_keywords():
    agent = _FakeAgent(["不该出现"])
    bot = _bot(agent, ated=False)
    bot.mp.hot = False
    bot.mp.reply_to_bot = True
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 0
    assert bot.mp.keyword_calls == 1


def test_hot_but_not_reply_to_bot_falls_back_to_keywords():
    agent = _FakeAgent(["不该出现"])
    bot = _bot(agent, ated=False)
    bot.mp.hot = True
    bot.mp.reply_to_bot = False
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert agent.chat_calls == 0


def test_message_is_remembered_for_thread_history():
    bot = _bot(_FakeAgent([]), ated=False)
    asyncio.run(bot.process_message(dict(GROUP_EVENT)))
    assert bot.mp.remembered == [GROUP_EVENT["message_id"]]


# ── 喊名字但没 @：以前既不进 AI、插话又当「没人叫我」让开，整句丢失 ──
def _text_event(text: str, message_id: int = 90002) -> dict:
    return {
        "message_type": "group",
        "group_id": 20001,
        "user_id": 10001,
        "message_id": message_id,
        "message_seq": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_called_by_name_without_at_goes_to_ai():
    agent = _FakeAgent(["我在呀"])
    bot = _bot(agent, ated=False)
    asyncio.run(bot.process_message(_text_event("君景呢")))
    assert agent.chat_calls == 1
    assert bot.mp.keyword_calls == 0
    assert bot.mp.sent == ["我在呀"]


def test_praising_by_name_falls_back_to_keywords():
    """只是背后夸它，不该被当成召唤强制回复。"""
    agent = _FakeAgent(["不该出现"])
    bot = _bot(agent, ated=False)
    asyncio.run(bot.process_message(_text_event("君景好可爱")))
    assert agent.chat_calls == 0
    assert bot.mp.keyword_calls == 1


def test_summon_attention_is_passed_to_interject():
    """判出来的注意力要传给插话器，它才能跳过被点名的那条、不重复计数。"""
    bot = _bot(_FakeAgent([]), ated=False)
    asyncio.run(bot.process_message(_text_event("君景在吗")))
    assert bot.interject.attentions == ["summon"]

