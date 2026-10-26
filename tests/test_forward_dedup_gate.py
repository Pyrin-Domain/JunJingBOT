"""转发去重闸门：回放（is_recover）也必须认「搬过了」这个结论。

事故背景（data/forward_dedup_log.jsonl 里 hit 后面紧跟同 message_id 的 record）：
断点每 10 秒才落一次盘，崩溃/重启会把最后 10 秒里**已经搬过**的消息再回放一遍。
旧代码对 is_recover 整道闸门都跳过，于是回放路径照样搬运 → 同一条聊天记录被搬两次。
"""
import asyncio
from types import SimpleNamespace

import pytest

from bot.services.forward import ForwardService


class _FakeMP:
    def __init__(self):
        self.images = []
        self.old_forward = True

    def check_is_old_forward(self, event):
        return self.old_forward

    def check_is_miniapp(self, event):
        return {}

    async def send_img(self, event=None, img_addr=None, summary=None):
        self.images.append(summary)


def _service(*, decision=None, matched=None):
    """造一个只关心闸门判定的 ForwardService。"""
    mp = _FakeMP()
    svc = ForwardService(mp=mp, nc=SimpleNamespace(), config=SimpleNamespace(
        reply=lambda key, default="": default
    ))
    svc.forwarded = []
    svc.recorded = []
    svc.gate_calls = 0

    async def fake_gate(event, miniapp_info=None, target_groupid=None):
        svc.gate_calls += 1
        return {"text": "old_forward|tsum9|x", "kind": "old_forward"}, dict(matched or {})

    async def fake_forward(event, target_groupid=None, task_userid=None):
        svc.forwarded.append(event.get("message_id"))
        return 2

    async def fake_record(fingerprint, event, target_groupid=None):
        svc.recorded.append(event.get("message_id"))

    svc.forward_dedup_gate = fake_gate
    svc.batch_forward_single_msg = fake_forward
    svc.forward_dedup_record = fake_record
    return svc, mp


EVENT = {
    "message_type": "group",
    "group_id": 20003,
    "user_id": 1397209352,
    "message_id": 289577067,
}


def _run(svc, **kwargs):
    return asyncio.run(svc.auto_forward(
        event=dict(EVENT),
        groupid_list=[20003],
        target_groupid=[20002, 20004],
        dedup=True,
        **kwargs,
    ))


def test_recover_does_not_reforward_content_already_recorded():
    """回放时命中「搬过了」→ 不能再搬一次（事故的直接原因）。"""
    svc, mp = _service(matched={"decision": "hit", "seen_here": True})
    assert _run(svc, is_recover=True) is True
    assert svc.forwarded == [], "回放命中后仍然转发了，就是重复搬运"
    assert svc.recorded == []
    # 回放是补历史，不是群里有人重复发 → 不该发【发过了喵】
    assert mp.images == []


def test_recover_skips_content_marked_elsewhere():
    svc, mp = _service(matched={"decision": "skip", "seen_here": False})
    assert _run(svc, is_recover=True) is True
    assert svc.forwarded == []
    assert mp.images == []


def test_recover_still_forwards_brand_new_content():
    """真正没处理过的消息在库里查不到指纹（miss）→ 回放照常搬。"""
    svc, mp = _service(matched={})
    assert _run(svc, is_recover=True) is True
    assert svc.forwarded == [289577067]
    assert svc.recorded == [289577067]


def test_live_hit_sends_notice_without_forwarding():
    svc, mp = _service(matched={"decision": "hit", "seen_here": True})
    assert _run(svc) is True
    assert svc.forwarded == []
    assert mp.images == ["[发过了喵]"]


def test_live_miss_forwards_and_records():
    svc, mp = _service(matched={})
    assert _run(svc) is True
    assert svc.forwarded == [289577067]
    assert svc.recorded == [289577067]
