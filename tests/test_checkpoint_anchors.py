"""断点恢复：SnowLuma 的 message_id 与 NapCat 的 message_seq 都要认。"""

import asyncio
from types import SimpleNamespace

from bot.app import BotApp


def _run(coro):
    return asyncio.run(coro)


class FakePipeline:
    def __init__(self):
        self.events = []

    async def process_message(self, event, is_recover=False):
        self.events.append((event, is_recover))


class FakeMP:
    def __init__(self, latest, checkpoint=None):
        self.user_id = 999
        self.check_point = checkpoint or {}
        self.latest = latest
        self.ensure_calls = 0

    async def ensure_connected(self):
        self.ensure_calls += 1

    async def get_group_latest_msg(self, group_id):
        return self.latest


class FakeAPI:
    def __init__(self, pages, groups=None):
        self.pages = list(pages)
        self.groups = groups if groups is not None else [{"group_id": 1}]
        self.calls = []

    async def get_group_list(self):
        return {"data": self.groups}

    async def get_group_msg_history(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"data": {"messages": list(self.pages.pop(0))}}


def _bot(pages, latest, checkpoint=None):
    bot = BotApp.__new__(BotApp)
    bot.mp = FakeMP(latest, checkpoint)
    bot.api = FakeAPI(pages)
    bot.pipeline = FakePipeline()
    bot.config = SimpleNamespace(history_anchor="message_id")
    return bot


def test_recover_process_uses_snowluma_message_id():
    latest = {"message_seq": 60, "message_id": -20}
    pages = [
        [
            latest,
            {"message_seq": 59, "message_id": -19},
            {"message_seq": 58, "message_id": -18},
        ]
    ]
    checkpoint_event = {"group_id": 1, "message_seq": 50, "message_id": -19}
    bot = _bot(pages, {"message_seq": 61, "message_id": -21})

    _run(bot.recover_process(event=checkpoint_event, latest=latest))

    # 历史接口拿到的是最新消息的两个游标，由后端挑一个发出去。
    assert bot.api.calls == [
        {
            "group_id": 1,
            "reverse_order": True,
            "count": 20,
            "message_seq": 60,
            "message_id": -20,
        }
    ]
    # -18 被回放；-19 命中断点，不再回放。
    assert [event["message_id"] for event, _ in bot.pipeline.events] == [-18]
    assert all(is_recover for _, is_recover in bot.pipeline.events)
    # 断点更新成当前最新消息。
    assert bot.mp.check_point["1"] == {"message_seq": 61, "message_id": -21}


def test_recover_process_accepts_legacy_single_anchor():
    pages = [
        [
            {"message_seq": 10, "message_id": 10},
            {"message_seq": 9, "message_id": 9},
        ]
    ]
    bot = _bot(pages, None)

    _run(
        bot.recover_process(
            event={"group_id": 1, "message_seq": 9},
            latest_msg_id=10,
        )
    )

    assert bot.api.calls[0]["message_seq"] == 10
    assert bot.api.calls[0]["message_id"] == 10
    assert bot.pipeline.events == []


def test_recover_from_checkpoint_skips_when_latest_is_checkpoint():
    latest = {"message_seq": 60, "message_id": -20}
    bot = _bot([], latest, checkpoint={"1": dict(latest)})

    _run(bot.recover_from_check_point())

    assert bot.api.calls == []
    assert bot.pipeline.events == []
    assert bot.mp.check_point["1"] == latest


def test_recover_from_checkpoint_spawns_replay_for_new_messages():
    latest = {"message_seq": 60, "message_id": -20}
    pages = [
        [
            latest,
            {"message_seq": 59, "message_id": -19},
            {"message_seq": 58, "message_id": -18},
        ]
    ]
    bot = _bot(pages, {"message_seq": 61, "message_id": -21})
    bot.mp.check_point = {"1": {"group_id": 1, "message_id": -19}}

    async def scenario():
        await bot.recover_from_check_point()
        tasks = list(getattr(asyncio.get_running_loop(), "_jj_background_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks)

    _run(scenario())

    assert len(bot.api.calls) == 1
    assert [event["message_id"] for event, _ in bot.pipeline.events] == [-18]


def test_reconnect_runs_checkpoint_recovery_after_start():
    latest = {"message_seq": 60, "message_id": -20}
    bot = _bot([], latest, checkpoint={"1": {"group_id": 1, "message_id": -19}})
    bot._recovery_ready = True
    calls = []

    async def fake_recover():
        calls.append(True)

    bot.recover_from_check_point = fake_recover

    _run(bot._on_connection_ready())

    assert calls == [True]


def test_reconnect_skips_recovery_before_start():
    latest = {"message_seq": 60, "message_id": -20}
    bot = _bot([], latest, checkpoint={"1": {"group_id": 1, "message_id": -19}})
    bot._recovery_ready = False
    calls = []

    async def fake_recover():
        calls.append(True)

    bot.recover_from_check_point = fake_recover

    _run(bot._on_connection_ready())

    assert calls == []


def test_recover_from_checkpoint_skips_group_already_recovering():
    latest = {"message_seq": 60, "message_id": -20}
    bot = _bot([], latest, checkpoint={"1": {"group_id": 1, "message_id": -19}})
    bot._recovering_groups = {"1"}

    _run(bot.recover_from_check_point())

    assert bot.api.calls == []
    assert bot.pipeline.events == []


# ══════════════════════════════════════════════════════════
# 真实后端形态：reverse_order=True 返回升序、锚点在**末位**
# （2026-10-26 实测：正确字段时锚点位置=末位；字段发错时退化成居中窗口）
# ══════════════════════════════════════════════════════════
class LoopingAPI(FakeAPI):
    """永远返回同一页，模拟「后端不认游标 / 游标原地打转」。"""

    def __init__(self, page):
        super().__init__([])
        self.page = page

    async def get_group_msg_history(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"data": {"messages": list(self.page)}}


def test_recover_does_not_replay_anchor_at_end():
    """锚点（最新那条）在页尾，不能被当成待回放的消息。"""
    latest = {"message_seq": 60, "message_id": 60}
    page = [
        {"message_seq": s, "message_id": s, "user_id": 1}
        for s in range(41, 61)  # 升序 41..60，锚点 60 在末位
    ]
    bot = _bot([page], {"message_seq": 61, "message_id": 61})

    _run(
        bot.recover_process(
            event={"group_id": 1, "message_seq": 50, "message_id": 50},
            latest=latest,
        )
    )

    replayed = [e["message_seq"] for e, _ in bot.pipeline.events]
    assert 60 not in replayed, "锚点本身被回放了"
    assert replayed == list(range(59, 50, -1)), "只回放断点之后的消息，从新到旧"


def test_recover_stops_when_page_only_has_cursor():
    """后端没往前走时立刻停，不要空翻 80 页。"""
    cursor = {"message_seq": 60, "message_id": 60}
    bot = _bot([], {"message_seq": 61, "message_id": 61})
    bot.api = LoopingAPI([{"message_seq": 60, "message_id": 60, "user_id": 1}])

    _run(
        bot.recover_process(
            event={"group_id": 1, "message_seq": 50, "message_id": 50},
            latest=cursor,
        )
    )

    assert bot.pipeline.events == []
    assert len(bot.api.calls) == 1


def test_recover_multi_page_never_skips_oldest():
    """多页翻动时，每页最旧那条不能因为「剔掉锚点」被跳过。"""
    latest = {"message_seq": 60, "message_id": 60}
    pages = [
        [
            {"message_seq": s, "message_id": s, "user_id": 1}
            for s in range(41, 61)
        ],
        [
            {"message_seq": s, "message_id": s, "user_id": 1}
            for s in range(21, 42)
        ],
        [],  # 到头了
    ]
    bot = _bot(pages, {"message_seq": 61, "message_id": 61})

    _run(
        bot.recover_process(
            event={"group_id": 1, "message_seq": 5, "message_id": 5},
            latest=latest,
        )
    )

    replayed = [e["message_seq"] for e, _ in bot.pipeline.events]
    # 两页翻完后应该是 59..21 全部回放到，且没有重复。
    assert replayed == list(range(59, 20, -1))
    assert len(replayed) == len(set(replayed))
