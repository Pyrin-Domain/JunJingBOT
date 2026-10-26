"""临时会话历史：合并、去重、敏感期、TTL 销毁、数量上限。"""
import time

from messaging.models import ChatMessage, Segment, Sender
from messaging.thread_history import (
    ThreadHistory,
    ThreadHistoryStore,
    merge_history,
)


def _msg(mid, text, *, me=False, qq="101", minute=0, quote=None):
    from datetime import datetime

    return ChatMessage(
        message_id=str(mid),
        time=datetime(2026, 10, 19, 12, minute),
        sender=Sender(qq=qq, name="我" if me else "群友", is_self=me),
        segments=[Segment("text", text=text)],
        quote=quote,
    )


def test_upsert_dedupes_and_prefers_richer_version():
    history = ThreadHistory(thread_id="g1")
    history.upsert(_msg(1, "你好"))
    richer = _msg(1, "你好")
    richer.quote = object()          # 带引用摘要的完整版本
    history.upsert(richer)
    assert len(history.messages) == 1
    assert history.messages[0].quote is not None


def test_upsert_trims_oldest_beyond_limit():
    history = ThreadHistory(thread_id="g1", max_messages=3)
    for index in range(5):
        history.upsert(_msg(index, f"消息{index}"))
    assert [m.message_id for m in history.messages] == ["2", "3", "4"]


def test_merge_history_prepends_older_buffered_messages():
    buffered = [_msg(1, "更早1"), _msg(2, "更早2"), _msg(5, "刚爬到的")]
    fetched = [_msg(5, "刚爬到的"), _msg(6, "当前")]
    merged = merge_history(buffered, fetched)
    assert [m.message_id for m in merged] == ["1", "2", "5", "6"]


def test_merge_history_keeps_newer_buffer_entries():
    """缓存里可能有刚发出去、历史接口还没返回的一条，要按时间排到后面。"""
    buffered = [_msg(1, "旧"), _msg(9, "比最高还新", minute=9)]
    fetched = [_msg(5, "f5", minute=5), _msg(6, "f6", minute=6)]
    merged = merge_history(buffered, fetched)
    assert [m.message_id for m in merged] == ["1", "5", "6", "9"]


def test_merge_history_without_buffer_returns_fetched():
    fetched = [_msg(1, "a")]
    assert merge_history([], fetched) == fetched


def test_hot_window():
    history = ThreadHistory(thread_id="g1")
    history.mark_bot_spoke(now=100.0)
    assert history.is_hot(now=110.0, hot_seconds=60) is True
    assert history.is_hot(now=200.0, hot_seconds=60) is False
    assert ThreadHistory(thread_id="g2").is_hot(now=100.0) is False


def test_ttl_expiry_and_manual_drop():
    store = ThreadHistoryStore(ttl_seconds=60)
    store.remember("g1", _msg(1, "hi"), now=1000.0)
    assert store.sweep(now=1030.0) == 0
    assert store.sweep(now=1100.0) == 1
    assert store.snapshot("g1") == []

    store.remember("g2", _msg(2, "hi"), now=2000.0)
    assert store.drop("g2") is True
    assert store.drop("g2") is False


def test_store_hot_flag_uses_real_clock():
    store = ThreadHistoryStore(hot_seconds=600)
    store.mark_bot_spoke("g1")
    assert store.is_hot("g1") is True
    assert store.is_hot("g1", hot_seconds=0) is False


def test_store_knows_which_message_is_mine():
    store = ThreadHistoryStore()
    store.remember("g1", _msg(1, "我说的", me=True, qq="10002"))
    store.remember("g1", _msg(2, "别人说的", qq="101"))
    assert store.is_bot_message("g1", "1") is True
    assert store.is_bot_message("g1", "2") is False
    assert store.is_bot_message("g1", "999") is False


def test_max_threads_evicts_least_recent():
    store = ThreadHistoryStore(max_threads=2, ttl_seconds=10 ** 6)
    store.remember("g1", _msg(1, "a"), now=100.0)
    store.remember("g2", _msg(2, "b"), now=200.0)
    store.remember("g3", _msg(3, "c"), now=300.0)
    assert store.get("g1") is None
    assert store.get("g3") is not None


def test_stats_shape():
    store = ThreadHistoryStore()
    store.remember("g1", _msg(1, "a"))
    stats = store.stats()
    assert stats[0]["thread_id"] == "g1"
    assert stats[0]["messages"] == 1
