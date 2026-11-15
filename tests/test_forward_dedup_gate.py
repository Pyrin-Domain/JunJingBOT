"""转发去重闸门：命中之后到底还发不发、要不要回一句。

两件事是分开的，别再揉成一个 `decision`：

* **目标群**：库里记着「这条内容发过哪些群」，闸门把它们从目标里减掉，剩下的照发
  —— 上次只发出去一半、后来新增了目标群，都能在这一次补齐；
* **回应**：只有「这个群自己就有记录」才回一个提示；回放不提示，目标群也不提示
  （内容本来就会送到它手里，回一句只会和卡片叠着刷屏）。

提示本身发的是一个**商城表情**（mface），不是本地图片 —— 表情自带表情名
（summary =「此条news已被其他群友发布过」）。没配商城表情才退回本地图片。
"""
import asyncio
from types import SimpleNamespace

import pytest

from bot.services.dedup import ForwardDedupStore
from bot.services.forward import ForwardService, GateOutcome
from messaging.meme import to_segments

#: 「这条发过了」提示用的商城表情（配置项 auto_forward.forwarded_already_mface）
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
        self.old_forward = True

    def check_is_old_forward(self, event):
        return self.old_forward

    def check_is_miniapp(self, event):
        return {}

    async def send_img(self, event=None, img_addr=None, summary=None):
        self.images.append(summary)

    async def send_message(self, event=None, message=None):
        self.messages.append(message)


def _service(*, targets=None, extra_reply=None, matched=None, mface=True):
    """造一个只关心「闸门结论怎么落地」的 ForwardService。

    :param targets: 闸门返回的剩余目标群；None = 原样返回传入的目标
    :param extra_reply: 闸门要求的回应；None = 不回
    :param mface: 是否配了商城表情；False = 退回本地图片那条老路
    """
    mp = _FakeMP()
    values = {"auto_forward.forwarded_already_mface": _MFACE} if mface else {}
    svc = ForwardService(mp=mp, nc=SimpleNamespace(), config=SimpleNamespace(
        reply=lambda key, default="": default,
        get=lambda key, default=None: values.get(key, default),
    ))
    svc.forwarded = []
    svc.forwarded_targets = []
    svc.recorded = []
    svc.gate_calls = 0

    async def fake_gate(
        event, miniapp_info=None, target_groupid=None, cur_group_is_target=False
    ):
        svc.gate_calls += 1
        rest = list(target_groupid or []) if targets is None else list(targets)
        return GateOutcome(
            verdict="hint" if extra_reply else "miss",
            targets=rest,
            extra_reply=extra_reply,
            fingerprint={"text": "old_forward|tsum9|x", "kind": "old_forward"},
            matched=dict(matched or {}),
        )

    async def fake_forward(event, target_groupid=None, task_userid=None):
        svc.forwarded.append(event.get("message_id"))
        svc.forwarded_targets.append(list(target_groupid or []))
        return len(target_groupid or [])

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


def _run(svc, target_groupid=None, **kwargs):
    return asyncio.run(svc.forward(
        event=dict(EVENT),
        groupid_list=[20003],
        target_groupid=[20002, 20004] if target_groupid is None else target_groupid,
        dedup=True,
        **kwargs,
    ))


def _sent_mface(mp) -> list[dict]:
    """把发出去的提示还原成消息段（只留 mface 段）。"""
    segments: list[dict] = []
    for message in mp.messages:
        segments.extend(
            seg for seg in (to_segments(message) or []) if seg.get("type") == "mface"
        )
    return segments


def test_recover_does_not_reforward_content_already_recorded():
    """回放时所有目标群都发过 → 一个都不发，也不回话。"""
    svc, mp = _service(targets=[], matched={"decision": "hit", "seen_here": True})
    assert _run(svc, is_recover=True) is True
    assert svc.forwarded == [], "回放命中后仍然转发了，就是重复搬运"
    assert svc.recorded == []
    # 回放是补历史，不是群里有人重复发 → 不该发提示
    assert mp.messages == []
    assert mp.images == []


def test_recover_still_forwards_brand_new_content():
    """真正没处理过的消息 → 目标群原样全发。"""
    svc, mp = _service(matched={})
    assert _run(svc, is_recover=True) is True
    assert svc.forwarded == [289577067]
    assert svc.forwarded_targets == [[20002, 20004]]
    assert svc.recorded == [289577067]


def test_live_hit_sends_notice_without_forwarding():
    """该群有记录、而且所有目标群也都发过 → 只回一个表情，不再搬。"""
    svc, mp = _service(targets=[], extra_reply="[发过了喵]")
    assert _run(svc) is True
    assert svc.forwarded == []
    assert mp.images == [], "提示已经是商城表情了，不该再发本地图片"
    assert _sent_mface(mp) == [
        {
            "type": "mface",
            "data": {
                "emoji_id": "2d831a9731974a5bd0fa57e892bc0bd8",
                "key": "fe89fd36de66bdc2",
                "summary": "[此条news已被其他群友发布过]",
                "emoji_package_id": 233590,
            },
        }
    ]


def test_live_miss_forwards_and_records():
    svc, mp = _service(matched={})
    assert _run(svc) is True
    assert svc.forwarded == [289577067]
    assert svc.recorded == [289577067]


def test_hit_fills_the_gaps_instead_of_stopping():
    """命中 ≠ 全停：库里发过的群剔掉，剩下的（新增目标群 / 上次没发成功的）照发。"""
    svc, mp = _service(targets=[20004], extra_reply="[发过了喵]")
    assert _run(svc) is True
    assert _sent_mface(mp), "该群有记录 → 还是要发提示"
    assert svc.forwarded_targets == [[20004]], "剩下的目标群应该补发"


def test_notice_falls_back_to_local_image_without_mface_config():
    """没配商城表情 → 退回老的本地图片，别让这条提示整个消失。"""
    svc, mp = _service(targets=[], extra_reply="[发过了喵]", mface=False)
    assert _run(svc) is True
    assert mp.messages == []
    assert mp.images == ["[发过了喵]"]


def test_notice_falls_back_when_mface_config_is_incomplete():
    """商城表情少一件（缺 key / emoji_id）→ 也退回本地图片。"""
    svc, mp = _service(targets=[], extra_reply="[发过了喵]", mface=False)
    svc.settings.get = lambda key, default=None: {"emoji_id": "abc"}  # 缺 key
    assert _run(svc) is True
    assert mp.messages == []
    assert mp.images == ["[发过了喵]"]


def test_extreme_case_no_targets_left_and_no_reply_does_nothing():
    svc, mp = _service(targets=[])
    assert _run(svc) is True
    assert svc.forwarded == []
    assert mp.messages == []
    assert mp.images == []


def test_message_from_own_target_group_never_reaches_target_list():
    """目标群同时是来源群：自己那一份必须先从目标里剔掉，不能搬回自己。"""
    svc, mp = _service(matched={})
    assert _run(svc, target_groupid=[20003, 20004]) is True
    assert svc.forwarded_targets == [[20004]]


def test_self_message_is_never_forwarded():
    """机器人自己发出去的消息回流（目标群同时也在来源群里）不能再搬一次。

    线上 17:35:47 从 1054955587 搬了一次，10 秒后机器人在 933109193 发的那张卡片
    又以 uid=自己 的身份进来，weak 指纹取不到图 → 判「没搬过」→ 搬去了另外两个群。
    """
    svc, mp = _service(matched={})
    mp.user_id = 3122793026
    event = dict(EVENT, user_id=3122793026)
    assert asyncio.run(
        svc.forward(
            event=event,
            groupid_list=[20003],
            target_groupid=[20002, 20004],
            dedup=True,
        )
    ) is False
    assert svc.gate_calls == 0, "自己的消息连闸门都不该查"
    assert svc.forwarded == []
    assert svc.recorded == []


def test_other_people_still_forwarded_when_self_check_is_on():
    svc, mp = _service(matched={})
    mp.user_id = 3122793026
    assert _run(svc) is True
    assert svc.forwarded == [289577067]


# ══════════════════════════════════════════════════════════
# 真闸门：targets 减法 + extra_reply 的条件
# ══════════════════════════════════════════════════════════
def _gate_service(tmp_path):
    store = ForwardDedupStore(
        db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}}
    )
    svc = ForwardService(
        mp=_FakeMP(),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    messages = [
        {
            "sender": {"card": "A"},
            "message": [
                {"type": "text", "data": {"text": "这件事真的离谱，我今天才知道"}},
                {"type": "text", "data": {"text": "离谱到我不知道怎么说了都"}},
            ],
        }
    ]

    async def fake_expand(event, kind="old_forward"):
        return messages, "test"

    async def fake_image(event, kind, messages=None):
        return [], {"error": "should_not_call"}

    svc._forward_kind = lambda event, miniapp_info=None: "old_forward"
    svc._expand_forward_for_dedup = fake_expand
    svc._fetch_forward_image_hashes = fake_image
    return svc, store


def _card_event(group_id, message_id):
    return {
        "message_type": "group",
        "group_id": group_id,
        "message_id": message_id,
        "message": [{"type": "forward", "data": {"id": f"fwd{message_id}"}}],
    }


def test_real_gate_subtracts_delivered_groups(tmp_path, monkeypatch):
    import bot.services.dedup as dedup_module

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    svc, store = _gate_service(tmp_path)
    try:
        first = _card_event(20003, 1)
        outcome = asyncio.run(
            svc.forward_dedup_gate(first, target_groupid=[20002, 20004])
        )
        assert outcome.targets == [20002, 20004], "第一次见，目标原样"
        assert outcome.extra_reply is None
        asyncio.run(
            svc.forward_dedup_record(
                outcome.fingerprint, first, target_groupid=[20002, 20004]
            )
        )

        # 同一份内容换个来源群再来：两个目标群都发过了 → 目标清空
        second = _card_event(20005, 2)
        outcome = asyncio.run(
            svc.forward_dedup_gate(second, target_groupid=[20002, 20004, 20006])
        )
        assert outcome.targets == [20006], "已发过的群要减掉，新增的群要保留"
        assert outcome.extra_reply is None, "这个群（20005）没有记录，不该提示"
    finally:
        store.close()


def test_real_gate_hints_when_the_group_itself_has_the_record(tmp_path, monkeypatch):
    import bot.services.dedup as dedup_module

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    svc, store = _gate_service(tmp_path)
    try:
        first = _card_event(20003, 1)
        outcome = asyncio.run(
            svc.forward_dedup_gate(first, target_groupid=[20002, 20004])
        )
        asyncio.run(
            svc.forward_dedup_record(
                outcome.fingerprint, first, target_groupid=[20002, 20004]
            )
        )
        # 同一个群里又发了一遍 → 有记录 → 回【发过了喵】
        again = asyncio.run(
            svc.forward_dedup_gate(_card_event(20003, 3), target_groupid=[20006])
        )
        assert again.extra_reply == "[发过了喵]"
        assert again.targets == [20006], "没发过的目标群照旧保留"

        # 已经被这次搬运覆盖过的目标群 → 减掉（避免重复发）
        covered = asyncio.run(
            svc.forward_dedup_gate(_card_event(20003, 5), target_groupid=[20002, 20004])
        )
        assert covered.targets == []

        # 但同一个群本身是目标群时（配置里两边都有）→ 静默
        as_target = asyncio.run(
            svc.forward_dedup_gate(
                _card_event(20003, 4),
                target_groupid=[20002, 20004],
                cur_group_is_target=True,
            )
        )
        assert as_target.extra_reply is None
    finally:
        store.close()
