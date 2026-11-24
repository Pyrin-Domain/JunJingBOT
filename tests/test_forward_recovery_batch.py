"""断点恢复两阶段批次：先统一入库，再统一发送（每份内容只发一次）。"""

import asyncio
from types import SimpleNamespace

from bot.services.forward import ForwardService, GateOutcome, RecoveryBatch, StagedForward

_MFACE = {
    "summary": "[此条news已被其他群友发布过]",
    "emoji_id": "2d831a9731974a5bd0fa57e892bc0bd8",
    "emoji_package_id": 233590,
    "key": "fe89fd36de66bdc2",
}


class _FakeMP:
    def __init__(self):
        self.images = []
        self.messages = []

    async def send_img(self, event=None, img_addr=None, summary=None):
        self.images.append(summary)

    async def send_message(self, event=None, message=None):
        self.messages.append(message)


def _service():
    mp = _FakeMP()
    svc = ForwardService(mp=mp, nc=SimpleNamespace(), config=SimpleNamespace(
        reply=lambda key, default="": default,
        get=lambda key, default=None: (_MFACE if key == "auto_forward.forwarded_already_mface" else default),
    ))
    svc.forwarded = []
    svc.forwarded_targets = []
    svc.recorded = []

    async def fake_forward(event, target_groupid=None, task_userid=None, **kwargs):
        svc.forwarded.append(event.get("message_id"))
        svc.forwarded_targets.append(list(target_groupid or []))
        return len(target_groupid or [])

    async def fake_batch_forward(event, target_groupid=None, task_userid=None, **kwargs):
        svc.forwarded.append(event.get("message_id"))
        svc.forwarded_targets.append(list(target_groupid or []))
        return len(target_groupid or [])

    async def fake_record(fingerprint, event, target_groupid=None, **kwargs):
        svc.recorded.append((event.get("message_id"), list(target_groupid or [])))

    svc.batch_forward_single_msg = fake_forward
    svc.forward_rebuilt = fake_batch_forward
    svc.forward_dedup_record = fake_record
    return svc, mp


def _event(group_id, message_id, user_id=1397209352):
    return {
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
    }


def test_staged_forward_merges_duplicates_by_key():
    """同一份内容（相同身份）合并为一份，目标群取并集。"""
    batch = RecoveryBatch()
    a = StagedForward(
        job=SimpleNamespace(event=_event(100, 1), target_groups=[20003], target_users=[]),
        outcome=GateOutcome(targets=[20003]),
        identities=("leaf:10001", "fwd:20002"),
        target_groups=(20003,),
        target_users=(),
        recorded_groups=(100,),
    )
    b = StagedForward(
        job=SimpleNamespace(event=_event(101, 2), target_groups=[20002], target_users=[]),
        outcome=GateOutcome(targets=[20002]),
        identities=("leaf:10001", "fwd:20002"),
        target_groups=(20002,),
        target_users=(),
        recorded_groups=(101,),
    )
    batch.add(a)
    batch.add(b)
    assert len(batch) == 1
    item = batch.drain()[0]
    assert item.target_groups == (20003, 20002)
    assert item.recorded_groups == (100, 101)


def test_batch_dedupe_and_send_once():
    """两份相同内容（来自两个来源群），合并后只发一次，且发给剩余目标。"""
    svc, mp = _service()
    batch = svc.begin_recovery_batch()

    # A 群和 B 群同时回放同一条记录，目标群都配了 20003，A 群自己就是来源之一
    ev_a = _event(100, 101)
    ev_b = _event(100, 102)
    job_a = SimpleNamespace(
        event=ev_a,
        target_groups=[20003],
        target_users=[],
        strip_tail_ads=None,
        blocked=False,
    )
    job_b = SimpleNamespace(
        event=ev_b,
        target_groups=[20003],
        target_users=[],
        strip_tail_ads=None,
        blocked=False,
    )

    staged_a = StagedForward(
        job=job_a,
        outcome=GateOutcome(targets=[20003], matched={"group_ids": [100]}),
        identities=("leaf:777",),
        target_groups=(20003,),
        target_users=(),
        recorded_groups=(100,),
        strip_tail_ads=None,
    )
    staged_b = StagedForward(
        job=job_b,
        outcome=GateOutcome(targets=[20003], matched={"group_ids": [100]}),
        identities=("leaf:777",),
        target_groups=(20003,),
        target_users=(),
        recorded_groups=(100,),
        strip_tail_ads=None,
    )
    batch.add(staged_a)
    batch.add(staged_b)
    assert len(batch) == 1

    item = batch.drain()[0]
    recorded = {str(g) for g in item.recorded_groups}
    targets = [g for g in dict.fromkeys(item.target_groups) if str(g) not in recorded]
    assert targets == [20003]
    svc.forwarded.clear()
    svc.forwarded_targets.clear()

    asyncio.run(svc._send_staged(item))
    # 只发一次（合并后一份内容）
    assert svc.forwarded == [101]
    assert svc.forwarded_targets == [[20003]]


def test_merged_sources_are_not_sent_back():
    """合并后的目标 = 目标1∪目标2 − 来源1∪来源2（来源群不再收一次）。"""
    svc, mp = _service()
    batch = svc.begin_recovery_batch()
    job_a = SimpleNamespace(
        event=_event(100, 201),
        target_groups=[101, 20003],
        target_users=[],
        strip_tail_ads=None,
        blocked=False,
    )
    job_b = SimpleNamespace(
        event=_event(101, 202),
        target_groups=[100, 20003],
        target_users=[],
        strip_tail_ads=None,
        blocked=False,
    )
    batch.add(
        StagedForward(
            job=job_a,
            outcome=GateOutcome(
                targets=[101, 20003], fingerprint={"text": "x|y", "kind": "old_forward"}
            ),
            identities=("leaf:888",),
            target_groups=(101, 20003),
            target_users=(),
            source_groups=("100",),
        )
    )
    batch.add(
        StagedForward(
            job=job_b,
            outcome=GateOutcome(
                targets=[100, 20003], fingerprint={"text": "x|y", "kind": "old_forward"}
            ),
            identities=("leaf:888",),
            target_groups=(100, 20003),
            target_users=(),
            source_groups=("101",),
        )
    )
    assert len(batch) == 1
    item = batch.drain()[0]
    assert item.source_groups == ("100", "101")
    assert set(item.target_groups) == {100, 101, 20003}

    captured: dict = {}

    async def fake_record(fingerprint, event, target_groupid=None, **kwargs):
        captured["targets"] = list(target_groupid or [])
        captured.update(kwargs)

    svc.forward_dedup_record = fake_record
    svc.forwarded.clear()
    svc.forwarded_targets.clear()

    asyncio.run(svc._send_staged(item))

    # 目标并集 {100,101,20003} − 来源并集 {100,101} = {20003}
    assert svc.forwarded_targets == [[20003]]
    assert captured["source_groups"] == ["100", "101"]


def test_recovery_gate_does_not_reserve_main_db():
    """recover 期间闸门不往主库认领（reserve=False），认领交给 RecoveryBatch。"""
    svc, mp = _service()
    svc.begin_recovery_batch()
    captured: dict = {}

    async def fake_gate(event, **kwargs):
        captured.update(kwargs)
        return GateOutcome(targets=[20003], fingerprint=None)

    svc.forward_dedup_gate = fake_gate
    job = SimpleNamespace(
        event=_event(100, 301),
        dedup=True,
        miniapp_info={},
        target_groups=[20003],
        target_users=[],
        self_group_is_target=False,
        is_recover=True,
        strip_tail_ads=None,
        blocked=False,
    )
    asyncio.run(svc._forward_record(job))

    assert captured["reserve"] is False


def test_batch_empty_when_no_items():
    svc, mp = _service()
    batch = svc.begin_recovery_batch()
    batch.finalize()
    items = batch.drain()
    assert items == []
    assert len(batch) == 0


def test_batch_sealed_after_flush():
    svc, mp = _service()
    batch = svc.begin_recovery_batch()
    batch.add(
        StagedForward(
            job=SimpleNamespace(event=_event(100, 1), target_groups=[20003], target_users=[]),
            outcome=GateOutcome(targets=[20003]),
            identities=("leaf:999",),
            target_groups=(20003,),
            target_users=(),
            recorded_groups=(),
            strip_tail_ads=None,
        )
    )
    batch.finalize()
    items = batch.drain()
    assert len(items) == 1
    assert batch.sealed
    # drain 清空后再取是空的
    assert batch.drain() == []
