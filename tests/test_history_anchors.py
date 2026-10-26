"""历史接口按后端选择游标，以及双游标区间比较。"""

import asyncio

from messaging.history import (
    get_history_events,
    get_long_history_backward,
    get_long_history_only_processor,
)
from transport.api import OneBotAPI


def _run(coro):
    return asyncio.run(coro)


class FakeConnection:
    def __init__(self, anchor: str):
        self.history_anchor = anchor
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None, timeout=None):
        self.calls.append((action, params or {}))
        return {"status": "ok", "data": {}}


def test_api_selects_snowluma_history_anchor():
    conn = FakeConnection("message_id")
    api = OneBotAPI(conn)

    _run(
        api.get_group_msg_history(
            group_id=1, message_seq=10, message_id=-20, reverse_order=True, count=5
        )
    )

    action, params = conn.calls[-1]
    assert action == "get_group_msg_history"
    assert params == {
        "group_id": 1,
        "reverse_order": True,
        "count": 5,
        "message_id": -20,
    }
    assert "message_seq" not in params


def test_api_selects_napcat_history_anchor():
    conn = FakeConnection("message_seq")
    api = OneBotAPI(conn)

    _run(api.get_friend_msg_history(user_id=2, message_seq=10, message_id=-20, count=3))

    action, params = conn.calls[-1]
    assert action == "get_friend_msg_history"
    assert params == {
        "user_id": 2,
        "reverse_order": False,
        "count": 3,
        "message_seq": 10,
    }
    assert "message_id" not in params


def test_api_single_anchor_is_used_for_both_backends():
    for anchor in ("message_seq", "message_id"):
        conn = FakeConnection(anchor)
        api = OneBotAPI(conn)
        _run(api.get_group_msg_history(group_id=1, message_id=7))
        params = conn.calls[-1][1]
        assert params.get(anchor) == 7


class FakeNC:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    async def get_group_msg_history(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"data": {"messages": list(self.pages.pop(0))}}

    async def get_friend_msg_history(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"data": {"messages": list(self.pages.pop(0))}}


class FakeMP:
    def __init__(self, nc):
        self.nc = nc
        self.api = nc
        self.user_id = 999

    async def ensure_connected(self):
        return None

    async def collect_message(self, raw, **kwargs):
        return raw


def test_get_history_events_passes_both_anchors():
    nc = FakeNC([[{"message_id": -1, "message_seq": 5}]])
    mp = FakeMP(nc)

    events = _run(
        get_history_events(
            mp,
            event={
                "message_type": "group",
                "group_id": 100,
                "message_id": -1,
                "message_seq": 5,
            },
            count=3,
        )
    )

    assert events == [{"message_id": -1, "message_seq": 5}]
    call = nc.calls[-1]
    assert call["group_id"] == 100
    assert call["message_id"] == -1
    assert call["message_seq"] == 5
    assert call["count"] == 3


def test_get_history_events_private_uses_friend_history():
    nc = FakeNC([[{"message_id": 1}]])
    mp = FakeMP(nc)

    _run(
        get_history_events(
            mp,
            event={"message_type": "private", "user_id": 200, "message_id": 1},
            count=2,
        )
    )

    call = nc.calls[-1]
    assert call["user_id"] == 200
    assert call["message_id"] == 1


def test_backward_history_matches_snowluma_message_id():
    pages = [
        [
            {"message_seq": 100, "message_id": 1000},
            {"message_seq": 101, "message_id": 1001},
        ],
        [
            {"message_seq": 101, "message_id": 1001},
            {"message_seq": 102, "message_id": 1002},
        ],
    ]
    nc = FakeNC(pages)
    mp = FakeMP(nc)

    # 终点只给 SnowLuma 的 message_id；message_seq 与它完全不同。
    part = _run(
        get_long_history_backward(mp, group_id=1, start_id=100, end_id=1001)
    )

    # 返回顺序与旧实现一致：新 → 旧。
    assert [item["message_id"] for item in part] == [1001, 1000]
    assert len(nc.calls) == 2


def test_only_processor_stops_on_message_id():
    pages = [
        [
            {"message_seq": 1, "message_id": -1},
            {"message_seq": 2, "message_id": -2},
        ],
        [
            {"message_seq": 2, "message_id": -2},
            {"message_seq": 3, "message_id": -3},
        ],
    ]
    nc = FakeNC(pages)
    mp = FakeMP(nc)
    seen = []

    async def processor(event, **params):
        seen.append((event["message_id"], params.get("tag")))

    _run(
        get_long_history_only_processor(
            mp,
            group_id=1,
            start_id=1,
            end_id=-3,
            processor=processor,
            params={"tag": "x"},
        )
    )

    # 第一页没有终点，第二页命中 SnowLuma 的 message_id。
    assert seen[0] == (-1, "x")
    assert seen[-1] == (-3, "x")
    assert len(nc.calls) == 2
