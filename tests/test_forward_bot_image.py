"""自动搬运：机器人白名单 vs 来源闸门的先后顺序。

出过的 bug：私聊里由白名单机器人（``bot_list[].type = private``）发来的图片
不会搬运。原因是 ``auto_forward`` 先用 ``userid_list``（任务私聊列表）筛来源，
而机器人图片的白名单判定排在它后面 —— 私聊没配 ``userid_list`` 就直接 return，
``type: private`` 变成死配置。

这里锁住四件事：
1. 私聊 + 白名单机器人 + 带图 → 搬运（不再被 userid_list 挡住）；
2. 私聊 + 非白名单 → 不搬；
3. 群聊 + 白名单机器人 + 来源群在 groupid_list → 照旧搬；
4. 群聊 + 白名单机器人 + 来源群不在 groupid_list → 照旧不搬（来源闸门仍然生效）。
"""

from __future__ import annotations

import asyncio

import pytest

from bot.services.forward import BatchForwardResult, ForwardService, SendOutcome
from bot.services.image_dedup import ForwardImageStore


class _FakeMP:
    """只实现 auto_forward 走到「机器人图片」分支所需的判定。"""

    def is_contain_image(self, _event) -> bool:
        return True

    def check_is_miniapp(self, _event) -> dict:
        return {}

    def check_is_old_forward(self, _event) -> bool:
        return False

    def check_is_multimsg_forward(self, _event):
        return None


def _image_event(*, message_type: str, user_id: int, group_id: int | None = None) -> dict:
    event = {
        "message_type": message_type,
        "user_id": user_id,
        "message": [{"type": "image", "data": {"file": "abc.image"}}],
    }
    if group_id is not None:
        event["group_id"] = group_id
    return event


BOT_ID = 1013098110
WHITELIST = {
    "enable": True,
    "bot_list": [{"bot_id": BOT_ID, "type": "all", "group_id": [], "user_id_list": []}],
}
TARGETS = [1079845768]


def _service(monkeypatch) -> tuple[ForwardService, list]:
    # 图片送达库用 :memory:（每测一个干净的库），不碰 data/forward_image.db
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=None,
        image_store=ForwardImageStore(db_path=":memory:"),
    )
    sent: list = []

    async def fake_batch(event, target_groupid=None, task_userid=None):
        sent.append((event.get("message_type"), event.get("user_id")))
        return len(target_groupid or [])

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)
    return service, sent


def _run(service, event, **kwargs) -> bool:
    return asyncio.run(
        service.auto_forward(
            event=event,
            groupid_list=[949950852],
            userid_list=[],
            target_groupid=TARGETS,
            task_userid=[],
            auto_img_forward_from_bot=WHITELIST,
            **kwargs,
        )
    )


def test_private_bot_image_is_forwarded(monkeypatch):
    service, sent = _service(monkeypatch)
    event = _image_event(message_type="private", user_id=BOT_ID)

    assert _run(service, event) is True
    assert sent == [("private", BOT_ID)]


def test_private_stranger_image_is_ignored(monkeypatch):
    service, sent = _service(monkeypatch)
    event = _image_event(message_type="private", user_id=999999)

    assert _run(service, event) is False
    assert sent == []


def test_group_bot_image_still_requires_source_group(monkeypatch):
    service, sent = _service(monkeypatch)

    inside = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    assert _run(service, inside) is True
    assert sent == [("group", BOT_ID)]

    sent.clear()
    outside = _image_event(message_type="group", user_id=BOT_ID, group_id=111111)
    assert _run(service, outside) is False
    assert sent == []


@pytest.mark.parametrize(
    "scope,message_type,should_match",
    [
        ("private", "private", True),
        ("private", "group", False),
        ("group", "group", True),
        ("group", "private", False),
        ("all", "private", True),
        ("all", "group", True),
    ],
)
def test_whitelist_scope_separates_group_and_private(
    monkeypatch, scope, message_type, should_match
):
    service, sent = _service(monkeypatch)
    entry = {"bot_id": BOT_ID, "type": scope, "group_id": [], "user_id_list": []}

    event = _image_event(
        message_type=message_type, user_id=BOT_ID, group_id=949950852
    )
    matched = asyncio.run(
        service.auto_forward(
            event=event,
            groupid_list=[949950852],
            target_groupid=TARGETS,
            auto_img_forward_from_bot={"enable": True, "bot_list": [entry]},
        )
    )

    assert matched is should_match
    assert bool(sent) is should_match


# ══════════════════════════════════════════════════════════
# 同一条消息只能搬一次（实时 + 断点回放会各来一遍）
# ══════════════════════════════════════════════════════════
def test_same_message_is_forwarded_only_once(monkeypatch):
    """线上实例：17:27:17 实时搬一次，17:27:46 回放又搬一次，进同三个群两遍。"""
    service, sent = _service(monkeypatch)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    event["message_id"] = 1652911697

    assert _run(service, event) is True
    # 回放拿到的还是同一条（message_id 相同）
    assert _run(service, event) is True

    assert sent == [("group", BOT_ID)], "同一条被搬了两次"


def test_different_messages_are_forwarded_separately(monkeypatch):
    service, sent = _service(monkeypatch)
    first = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    first["message_id"] = 111
    second = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    second["message_id"] = 222

    _run(service, first)
    _run(service, second)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]


def test_real_id_wins_over_message_id(monkeypatch):
    """NapCat 重启后 message_id 会变，real_id 不变 —— 靠它认出是同一条。"""
    service, sent = _service(monkeypatch)
    before = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    before.update({"message_id": 111, "real_id": 777760})
    after = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    after.update({"message_id": 999, "real_id": 777760})

    _run(service, before)
    _run(service, after)

    assert sent == [("group", BOT_ID)], "同一个 real_id 被当成两条消息"


def test_event_without_id_is_not_memorized(monkeypatch):
    """没有 message_id / real_id 时宁可不记忆，也不能凭错误身份误杀。"""
    service, sent = _service(monkeypatch)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)

    _run(service, event)
    _run(service, event)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]


def test_delivery_expires_after_keep_days(monkeypatch):
    """超过保留天数的送达记录会被清掉，同一条仍会搬（真的被重新发送时）。"""
    service, sent = _service(monkeypatch)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    event["message_id"] = 1652911697

    _run(service, event)
    assert sent == [("group", BOT_ID)]

    # 把送达记录的时间戳往前拨到过期
    store = service._get_image_store()
    conn = store._ensure_conn()
    conn.execute("UPDATE forward_image_delivery SET last_seen = 0")
    conn.commit()
    store._last_prune = 0.0
    store.keep_days = 1
    store._maybe_prune()

    _run(service, event)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]


def test_reload_image_dedup_rebuilds_lazy_store():
    service = ForwardService(
        mp=_FakeMP(), api=None, settings={"forward_image_dedup": {"enable": True}}
    )
    store = service._get_image_store()
    store._ensure_conn()

    service.reload_image_dedup()

    assert service._image_store is None


def test_reload_image_dedup_keeps_injected_store():
    store = ForwardImageStore(db_path=":memory:")
    service = ForwardService(mp=_FakeMP(), api=None, image_store=store)
    try:
        service.reload_image_dedup()
        assert service._image_store is store
    finally:
        store.close()


def test_image_dedup_can_be_disabled(monkeypatch):
    """关闭查重后：不查送达库、不登记，同一张图每次都会搬。"""
    config = {"forward_image_dedup": {"enable": False}}
    store = ForwardImageStore(db_path=":memory:", settings=config)
    service = ForwardService(
        mp=_FakeMP(), api=None, settings=config, image_store=store
    )
    sent: list = []

    async def fake_batch(event, target_groupid=None, task_userid=None):
        sent.append((event.get("message_type"), event.get("user_id")))
        return len(target_groupid or [])

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)

    _run(service, event)
    _run(service, event)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]
    assert store.count() == 0


def test_zero_real_id_falls_back_to_message_id(monkeypatch):
    """real_id 可能是 0 的占位值：不能把所有消息都当成同一条。"""
    service, sent = _service(monkeypatch)
    for mid in (111, 222):
        event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
        event.update({"message_id": mid, "real_id": 0})
        _run(service, event)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]


# ══════════════════════════════════════════════════════════
# 实时事件的字段和回放事件的字段不一样（线上 19:01 / 19:02 重复搬的现实原因）
# ══════════════════════════════════════════════════════════
def test_replay_after_live_is_skipped_when_real_id_only_exists_in_replay(monkeypatch):
    """SnowLuma 实时事件没有 real_id，历史接口有（= message_id）。

    以前只挑一个身份键：实时按 message_id 存、回放按 real_id 查 → 永远查不到，
    同一张图 42 秒内被搬两次。
    """
    service, sent = _service(monkeypatch)
    live = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    live["message_id"] = 494532601  # 实时：没有 real_id

    replayed = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    replayed.update({"message_id": 494532601, "real_id": 494532601})  # 历史：带 real_id

    assert _run(service, live) is True
    assert _run(service, replayed, is_recover=True) is True

    assert sent == [("group", BOT_ID)], "回放把实时已经搬过的图又搬了一次"


def test_live_after_replay_is_also_skipped(monkeypatch):
    """反过来（先回放、后实时）也要认得出来。"""
    service, sent = _service(monkeypatch)
    replayed = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    replayed.update({"message_id": 494532601, "real_id": 494532601})
    live = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    live["message_id"] = 494532601

    _run(service, replayed, is_recover=True)
    _run(service, live)

    assert sent == [("group", BOT_ID)]


def test_message_id_alone_still_separates_messages(monkeypatch):
    """没有 real_id 时不能把所有消息当成同一条。"""
    service, sent = _service(monkeypatch)
    for mid in (111, 222):
        event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
        event["message_id"] = mid
        _run(service, event)

    assert sent == [("group", BOT_ID), ("group", BOT_ID)]


# ══════════════════════════════════════════════════════════
# 图片门也按目标做减法（送过的群不重发，新群补发）
# ══════════════════════════════════════════════════════════
def test_image_delivery_is_tracked_per_target_group(monkeypatch):
    service, sent = _service(monkeypatch)
    calls: list = []
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    event["message_id"] = 333

    async def fake_batch(event, target_groupid=None, task_userid=None):
        calls.append([str(g) for g in (target_groupid or [])])
        return len(target_groupid or [])

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)

    def _run_targets(targets):
        return asyncio.run(
            service.forward(
                event=event,
                groupid_list=[949950852],
                target_groupid=targets,
                task_userid=[],
                auto_img_forward_from_bot=WHITELIST,
            )
        )

    assert _run_targets([20002, 20004]) is True
    # 同一张图再出现（断点回放）：两个群都送过 → 一个都不发
    assert _run_targets([20002, 20004]) is True
    # 后来新增了目标群 → 只补新的那个
    assert _run_targets([20002, 20004, 20006]) is True

    assert calls == [["20002", "20004"], ["20006"]]


def test_batch_forward_waits_and_counts_actual_successes(monkeypatch):
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=None,
        image_store=ForwardImageStore(db_path=":memory:"),
    )
    completed: list[str] = []

    async def fake_forward(event, message_type=None, group_id=None, user_id=None):
        await asyncio.sleep(0)
        target = f"{message_type}:{group_id or user_id}"
        completed.append(target)
        return group_id != 20003

    monkeypatch.setattr(service, "forward_single_msg", fake_forward)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)

    count = asyncio.run(
        service.batch_forward_single_msg(
            event, target_groupid=[20002, 20003], task_userid=[10001]
        )
    )

    assert count.succeed_id == [20002, 10001]
    assert count.failed_id == [20003]
    assert count.succeeded == 2
    assert completed == ["group:20002", "group:20003", "private:10001"]


def test_batch_forward_reports_retcode_and_db_idx(monkeypatch):
    """返回值要带逐目标 retcode 和库内主键（限流重试按 idx 回写）。"""
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=None,
        image_store=ForwardImageStore(db_path=":memory:"),
    )

    async def fake_forward(event, message_type=None, group_id=None, user_id=None):
        if group_id == 20003:
            return SendOutcome(ok=False, retcode=100)
        return SendOutcome(ok=True)

    monkeypatch.setattr(service, "forward_single_msg", fake_forward)
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)

    result = asyncio.run(
        service.batch_forward_single_msg(
            event, target_groupid=[20002, 20003], db_idx=42
        )
    )

    assert result.succeed_id == [20002]
    assert result.failed_id == [20003]
    assert result.retcodes["20003"] == 100
    assert result.rate_limited == [20003]
    assert result.db_idx == 42


def test_partial_image_delivery_remembers_successes(monkeypatch):
    """部分成功：成功的目标立刻入库，失败的（非限流）不登记，下次只补失败项。

    旧行为是「有一个失败就整批不登记」，导致已成功的群下次重复收到。
    """
    service, _sent = _service(monkeypatch)
    calls: list[list[str]] = []
    outcomes = iter(
        [
            BatchForwardResult(succeed_id=[20002], failed_id=[20004], attempted=2),
            BatchForwardResult(succeed_id=[20004], failed_id=[], attempted=1),
            None,
        ]
    )
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    event["message_id"] = 444

    async def fake_batch(event, target_groupid=None, task_userid=None):
        outcome = next(outcomes)
        if outcome is None:
            raise AssertionError("全部成功后不应再次发送")
        calls.append(list(target_groupid or []))
        return outcome

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)

    def forward_to(targets):
        return asyncio.run(
            service.forward(
                event=event,
                groupid_list=[949950852],
                target_groupid=targets,
                task_userid=[],
                auto_img_forward_from_bot=WHITELIST,
            )
        )

    assert forward_to([20002, 20004]) is True
    # 20002 已登记；20004 失败未登记 → 第二次只补 20004
    assert forward_to([20002, 20004]) is True
    # 两个都登记了 → 第三次一个都不发
    assert forward_to([20002, 20004]) is True
    assert calls == [[20002, 20004], [20004]]


def test_rate_limited_image_is_retried_and_recorded(monkeypatch):
    """retcode=100 的目标入队；re_send 成功后补登记送达。"""
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=None,
        image_store=ForwardImageStore(db_path=":memory:"),
    )
    event = _image_event(message_type="group", user_id=BOT_ID, group_id=949950852)
    event["message_id"] = 555
    store = service._get_image_store()
    sent: list = []
    failed_once: set = set()

    async def fake_forward(event, message_type=None, group_id=None, user_id=None):
        target = group_id if message_type == "group" else user_id
        sent.append(target)
        if target == 20004 and target not in failed_once:
            # 20004 第一次限流；20002 正常成功
            failed_once.add(target)
            return SendOutcome(ok=False, retcode=100)
        return SendOutcome(ok=True)

    async def fake_batch(event, target_groupid=None, task_userid=None):
        result = BatchForwardResult(attempted=2)
        for group_id in target_groupid or []:
            outcome = await fake_forward(event, "group", group_id=group_id)
            if outcome.ok:
                result.succeed_id.append(group_id)
            else:
                result.failed_id.append(group_id)
                result.retcodes[str(group_id)] = outcome.retcode
        return result

    monkeypatch.setattr(service, "batch_forward_single_msg", fake_batch)
    monkeypatch.setattr(service, "forward_single_msg", fake_forward)
    # 实时路径默认会起后台重试任务；这里手动调 re_send，先关掉后台任务
    monkeypatch.setattr(service, "_schedule_retry", lambda: None)

    asyncio.run(
        service.forward(
            event=event,
            groupid_list=[949950852],
            target_groupid=[20002, 20004],
            task_userid=[],
            auto_img_forward_from_bot=WHITELIST,
        )
    )
    # 20002 已登记；20004 限流在队列里
    assert store.delivered(["message_id:949950852:555"], ["g:20002"]) == {"g:20002"}
    assert len(service._retry_queue) == 1

    asyncio.run(service.re_send(interval=0, max_rounds=2))

    assert service._retry_queue == []
    assert store.delivered(
        ["message_id:949950852:555"], ["g:20002", "g:20004"]
    ) == {"g:20002", "g:20004"}
