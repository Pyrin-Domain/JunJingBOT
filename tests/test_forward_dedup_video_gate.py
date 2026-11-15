# -*- coding: utf-8 -*-
"""闸门里的视频分支：纯视频 weak 只走视频指纹，不去下图片。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.services.dedup import ForwardDedupStore
from bot.services.forward import ForwardService


class _FakeMP:
    pass


def _service(tmp_path):
    store = ForwardDedupStore(
        db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}}
    )
    return ForwardService(
        mp=_FakeMP(),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    ), store


def _event():
    return {
        "message_type": "group",
        "group_id": 20003,
        "message_id": 123,
        "message": [{"type": "forward", "data": {"id": "fwd"}}],
    }


def test_gate_video_only_uses_video_not_image(tmp_path, monkeypatch):
    # 别把测试事件写进生产去重日志
    import bot.services.dedup as dedup_module

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    svc, store = _service(tmp_path)
    messages = [
        {
            "sender": {"card": "A"},
            "message": [{"type": "video", "data": {"file": "x.mp4", "url": "https://x"}}],
        }
    ]
    calls = {"image": 0, "video": 0}
    sig = {
        "sig": "a" * 64,
        "cover": "a" * 32,
        "frame10": "b" * 32,
        "byte_hash": "c" * 64,
        "bytes": 100,
    }

    async def fake_expand(event, kind="old_forward"):
        return messages, "test"

    async def fake_image(event, kind, messages=None):
        calls["image"] += 1
        return [], {"error": "should_not_call"}

    async def fake_video(event, kind, messages=None):
        calls["video"] += 1
        return [sig], {"items": [{"ok": True}], "url_cnt": 1}

    svc._forward_kind = lambda event, miniapp_info=None: "old_forward"
    svc._expand_forward_for_dedup = fake_expand
    svc._fetch_forward_image_hashes = fake_image
    svc._fetch_forward_video_hashes = fake_video

    outcome = asyncio.run(svc.forward_dedup_gate(_event(), target_groupid=[20004]))
    assert calls == {"image": 0, "video": 1}
    assert outcome.fingerprint["weak"] is True
    assert outcome.fingerprint["images"] == []
    assert len(outcome.fingerprint["videos"]) == 1
    assert outcome.matched == {}
    assert outcome.targets == [20004], "第一次见，目标原样保留"

    asyncio.run(
        svc.forward_dedup_record(outcome.fingerprint, _event(), target_groupid=[20004])
    )
    again = asyncio.run(svc.forward_dedup_gate(_event(), target_groupid=[20004]))
    assert again.matched and again.matched["decision"] == "hit"
    assert again.targets == [], "已经发过的群要减掉"
    assert calls == {"image": 0, "video": 2}
