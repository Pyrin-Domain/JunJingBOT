"""断点恢复：SnowLuma 的 message_id 与 NapCat 的 message_seq 都要认。

2026-10-30 的事故：SnowLuma 重启后关闭了登录历史同步，不带游标取「最新一条」
返回的是它本地观测过的最新消息——正好等于断点本身。旧实现先取最新再往回翻，
于是每个群都判成「最新消息已是断点」，整晚的消息被跳过。

但「从后往前翻」对 NapCat 仍然是必须的：它的 message_id / message_seq 每次
重启都会重新分配，断点里的旧 id 不能当游标。两种策略由
``BackendCapabilities.history_latest_is_authoritative`` 选择：

- NapCat：True  -> 先取最新（现取的 id），再往回翻到断点；
- SnowLuma：False -> 不信任「最新」，从断点往后翻。
"""

import asyncio
from types import SimpleNamespace

from bot.app import BotApp


def _run(coro):
    return asyncio.run(coro)


def _msg(seq, user_id=1, **extra):
    return {"message_seq": seq, "message_id": seq, "user_id": user_id, **extra}


class FakePipeline:
    def __init__(self):
        self.events = []

    async def process_message(self, event, is_recover=False):
        self.events.append((event, is_recover))


class FakeMP:
    def __init__(self, checkpoint=None, latest=None):
        self.user_id = 999
        self.check_point = checkpoint or {}
        self.latest = latest
        self.latest_calls = 0
        self.ensure_calls = 0

    async def ensure_connected(self):
        self.ensure_calls += 1

    async def get_group_latest_msg(self, group_id):
        # SnowLuma 恢复流程不应该再问「最新一条」：它的答案可能只是本地观测值。
        # NapCat 则必须问（id 每次重启都会变，只能用现取的最新当游标）。
        self.latest_calls += 1
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
        # 真机响应长这样；抽象层按 status/retcode 判成功，缺了会被当成失败。
        return {"status": "ok", "retcode": 0, "data": {"messages": list(self.pages.pop(0))}}


def _bot(pages, latest=None, checkpoint=None, backend="snowluma"):
    bot = BotApp.__new__(BotApp)
    bot.mp = FakeMP(checkpoint, latest)
    bot.api = FakeAPI(pages)
    bot.pipeline = FakePipeline()
    bot.config = SimpleNamespace(history_anchor="message_id", backend=backend)
    return bot


async def _drain_background():
    tasks = list(getattr(asyncio.get_running_loop(), "_jj_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks)


def _recover(bot):
    async def scenario():
        await bot.recover_from_check_point()
        await _drain_background()

    _run(scenario())


# ══════════════════════════════════════════════════════════
# 回归：不能相信「最新一条」
# ══════════════════════════════════════════════════════════
def test_recovery_does_not_trust_backend_latest_when_it_equals_checkpoint():
    """后端说最新一条就是断点，但断点之后仍有消息：必须回放。

    复刻 SnowLuma：无游标 ``get_group_msg_history(count=1)`` 返回本地观测到的
    最新消息（= 断点），真正的更新消息只能用断点自己的 message_id 往后翻。
    """
    checkpoint = {"group_id": 1, "message_seq": 60, "message_id": -20}
    latest = dict(checkpoint)  # 后端本地观测到的「最新」就是断点
    pages = [
        [
            checkpoint,
            {"message_seq": 61, "message_id": -21, "user_id": 2},
            {"message_seq": 62, "message_id": -22, "user_id": 3},
        ],
        [],
    ]
    bot = _bot(pages, latest=latest, checkpoint={"1": checkpoint}, backend="snowluma")

    _recover(bot)

    assert bot.mp.latest_calls == 0, "SnowLuma 恢复流程不应再用本地观测的「最新一条」做判据"
    assert [event["message_id"] for event, _ in bot.pipeline.events] == [-22, -21]
    assert all(is_recover for _, is_recover in bot.pipeline.events)
    assert bot.mp.check_point["1"]["message_id"] == -22


def test_recovery_advances_checkpoint_when_nothing_new():
    checkpoint = {"group_id": 1, "message_seq": 60, "message_id": -20}
    bot = _bot([[checkpoint]], checkpoint={"1": checkpoint})

    _recover(bot)

    assert bot.pipeline.events == []
    assert bot.mp.check_point["1"] is checkpoint


# ══════════════════════════════════════════════════════════
# 并发：别把十几个群同时压给串行节流的后端
# ══════════════════════════════════════════════════════════
def test_recovery_probes_history_cursor_once():
    """游标只探测一次，不能每个群各探一遍。"""
    checkpoint = {
        "1": {"group_id": 1, "message_id": 1},
        "2": {"group_id": 2, "message_id": 2},
    }
    bot = _bot([], checkpoint=checkpoint)
    bot.api.groups = [{"group_id": 1}, {"group_id": 2}]
    calls = []

    async def verify(group_id=None):
        calls.append(group_id)

    async def fake_recover(event, **kwargs):
        return None

    bot.api.verify_history_cursor = verify
    bot.recover_process = fake_recover

    _recover(bot)

    assert calls == [1]


def test_recovery_limits_concurrent_groups():
    checkpoint = {
        str(i): {"group_id": i, "message_id": i} for i in range(1, 7)
    }
    bot = _bot([], checkpoint=checkpoint)
    bot.api.groups = [{"group_id": i} for i in range(1, 7)]
    active = 0
    peak = 0

    async def fake_recover(event, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    bot.recover_process = fake_recover

    _recover(bot)

    assert peak <= 3
    assert active == 0


# ══════════════════════════════════════════════════════════
# NapCat：最新可信，但 id 不可跨重启 → 从最新往前翻
# ══════════════════════════════════════════════════════════
def test_napcat_walks_backward_from_fresh_latest():
    """NapCat 重启后 message_id 全变，只能用现取的最新当游标。

    断点里存的是上一次运行的 id（111）；这次重启后同一条消息的
    message_id 变成 9110，只有 real_id（跨重启稳定）还是 777760。
    恢复必须先取最新、再往回翻到 real_id 命中为止。
    """
    checkpoint = {
        "group_id": 1,
        "message_seq": 111,
        "message_id": 111,
        "real_id": 777760,
    }
    latest = {
        "group_id": 1,
        "message_seq": 9914,
        "message_id": 9914,
        "real_id": 777764,
    }
    pages = [
        [
            {"message_seq": 9912, "message_id": 9912, "real_id": 777762, "user_id": 2},
            {"message_seq": 9913, "message_id": 9913, "real_id": 777763, "user_id": 2},
            latest,
        ],
        [
            {"message_seq": 9110, "message_id": 9110, "real_id": 777760, "user_id": 1},
            {"message_seq": 9111, "message_id": 9111, "real_id": 777761, "user_id": 1},
            {"message_seq": 9912, "message_id": 9912, "real_id": 777762, "user_id": 2},
        ],
    ]
    bot = _bot(pages, latest=latest, checkpoint={"1": checkpoint}, backend="napcat")

    _recover(bot)

    assert bot.mp.latest_calls == 2, "NapCat 要先取最新，找到断点后再取一次推进断点"
    assert bot.api.calls[0]["reverse_order"] is True
    assert bot.api.calls[0]["message_seq"] == 9914, "游标必须用现取的最新 id"
    # 从新到旧回放：777764 是起翻用的 latest，它也是「断点之后才到的消息」，
    # 必须一起回放（否则断点推进到它之后，这条就永久丢了）。
    # real_id=777760 是断点本身，不重放。
    assert [e["real_id"] for e, _ in bot.pipeline.events] == [777764, 777763, 777762, 777761]
    assert bot.mp.check_point["1"] == latest


def test_napcat_skips_when_latest_really_is_checkpoint():
    """NapCat 的「最新」来自服务端；相等就真的没有新消息。"""
    checkpoint = {
        "group_id": 1,
        "message_seq": 60,
        "message_id": 60,
        "real_id": 777760,
    }
    bot = _bot(
        [],
        latest=dict(checkpoint),
        checkpoint={"1": checkpoint},
        backend="napcat",
    )

    _recover(bot)

    assert bot.mp.latest_calls == 1
    assert bot.api.calls == []
    assert bot.pipeline.events == []
    assert bot.mp.check_point["1"] is checkpoint


def test_latest_owned_by_bot_neither_replays_nor_advances():
    """起翻用的 latest 是 Bot 自己发的：不回放，也不推进断点。

    这是「把 latest 补回来」的安全边界。Bot 自己的消息说明它当时还活着，
    与断点的先后关系不可靠（可能已经处理过），宁可这次不推进，下次重启再判。
    """
    checkpoint = {
        "group_id": 1,
        "message_seq": 111,
        "message_id": 111,
        "real_id": 777760,
    }
    latest = {
        "group_id": 1,
        "message_seq": 9914,
        "message_id": 9914,
        "real_id": 777764,
        "user_id": 999,  # FakeMP.user_id：就是 Bot 自己
    }
    pages = [
        [
            {"message_seq": 9913, "message_id": 9913, "real_id": 777763, "user_id": 2},
            latest,
        ],
        [
            checkpoint,
            {"message_seq": 9913, "message_id": 9913, "real_id": 777763, "user_id": 2},
        ],
    ]
    bot = _bot(pages, latest=latest, checkpoint={"1": checkpoint}, backend="napcat")

    _recover(bot)

    assert bot.pipeline.events == []
    assert bot.mp.check_point["1"] is checkpoint


# ══════════════════════════════════════════════════════════
# 方向与游标
# ══════════════════════════════════════════════════════════
def test_recover_process_walks_forward_with_both_cursors():
    checkpoint = {"group_id": 1, "message_seq": 50, "message_id": -19}
    pages = [
        [
            checkpoint,
            {"message_seq": 51, "message_id": -20, "user_id": 2},
            {"message_seq": 52, "message_id": -21, "user_id": 2},
        ],
        [],
    ]
    bot = _bot(pages)

    _run(bot.recover_process(event=checkpoint))

    # 游标字段由抽象层按后端能力挑：SnowLuma 只发 message_id。
    # 关键不是「两个都发」，而是**不能发错的那个** —— 后端会静默忽略并返回
    # 居中窗口，翻页就原地打转了。
    assert bot.api.calls[0] == {
        "group_id": 1,
        "reverse_order": False,
        "count": 20,
        "message_id": -19,
    }
    assert [event["message_id"] for event, _ in bot.pipeline.events] == [-21, -20]


def test_recover_multi_page_never_skips_boundary_message():
    """跨页时每页第一条（上一页的游标）要被剔除，但不丢任何一条真实消息。"""
    checkpoint = {"group_id": 1, "message_seq": 20, "message_id": 20}
    pages = [
        [_msg(s) for s in range(20, 41)],  # 锚点 20 在首位
        [_msg(s) for s in range(40, 61)],  # 锚点 40 在首位
        [],
    ]
    bot = _bot(pages)

    _run(bot.recover_process(event=checkpoint))

    replayed = [e["message_seq"] for e, _ in bot.pipeline.events]
    assert replayed == list(range(60, 20, -1))
    assert len(replayed) == len(set(replayed))


def test_recover_stops_when_page_only_has_cursor():
    """后端没往前走时立刻停，不要空翻 80 页。"""
    checkpoint = {"message_seq": 60, "message_id": 60}
    bot = _bot([[checkpoint]])

    _run(bot.recover_process(event=checkpoint))

    assert bot.pipeline.events == []
    assert len(bot.api.calls) == 1


def test_recover_stops_at_bot_own_message():
    checkpoint = {"group_id": 1, "message_seq": 10, "message_id": 10}
    pages = [[_msg(s, user_id=999 if s == 12 else 1) for s in range(10, 16)], []]
    bot = _bot(pages)

    _run(bot.recover_process(event=checkpoint))

    # 从新到旧回放：15、14、13 回放，遇到 12（Bot 自己）就停。
    assert [e["message_seq"] for e, _ in bot.pipeline.events] == [15, 14, 13]


# ══════════════════════════════════════════════════════════
# 触发时机
# ══════════════════════════════════════════════════════════
def test_recover_from_checkpoint_skips_group_already_recovering():
    checkpoint = {"group_id": 1, "message_id": -19}
    bot = _bot([], checkpoint={"1": checkpoint})
    bot._recovering_groups = {"1"}

    _run(bot.recover_from_check_point())

    assert bot.api.calls == []
    assert bot.pipeline.events == []


def test_recover_from_checkpoint_drops_groups_not_in_group_list():
    checkpoint = {"group_id": 2, "message_id": -19}
    bot = _bot([], checkpoint={"2": checkpoint})

    _run(bot.recover_from_check_point())

    assert bot.mp.check_point == {}
    assert bot.api.calls == []


def test_reconnect_runs_checkpoint_recovery_after_start():
    checkpoint = {"group_id": 1, "message_id": -19}
    bot = _bot([], checkpoint={"1": checkpoint})
    bot._recovery_ready = True
    calls = []

    async def fake_recover():
        calls.append(True)

    bot.recover_from_check_point = fake_recover

    _run(bot._on_connection_ready())

    assert calls == [True]


def test_reconnect_skips_recovery_before_start():
    checkpoint = {"group_id": 1, "message_id": -19}
    bot = _bot([], checkpoint={"1": checkpoint})
    bot._recovery_ready = False
    calls = []

    async def fake_recover():
        calls.append(True)

    bot.recover_from_check_point = fake_recover

    _run(bot._on_connection_ready())

    assert calls == []
