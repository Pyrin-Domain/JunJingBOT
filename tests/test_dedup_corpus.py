# -*- coding: utf-8 -*-
"""原始内容存档：入库时存一份「清洗前」的内容，以后改规则能重算。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from bot.services.dedup import ForwardDedupStore, build_fingerprint, fingerprint_of, tokenize


def _store(tmp_path) -> ForwardDedupStore:
    return ForwardDedupStore(db_path=tmp_path / "d.db", settings={"forward_dedup": {}})


def _text(body: str) -> dict:
    return {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": body}}]}


def _link(url: str) -> dict:
    return {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": url}}]}


def _fp_text(messages: list) -> str:
    return build_fingerprint({}, "old_forward", forward_messages=messages)["text"]


# ══════════════════════════ 存取 ══════════════════════════
def test_save_and_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    try:
        messages = [_text("正文"), _link("https://pd.qq.com/s/1")]
        text = _fp_text(messages)
        assert asyncio.run(store.save_corpus(text, "old_forward", messages, at=1793000000)) is True

        content_hash = fingerprint_of(tokenize(text))["content_hash"]
        assert store.load_corpus(content_hash) == messages
        assert store.load_corpus("不存在") is None
    finally:
        store.close()


def test_same_content_saved_once(tmp_path):
    """同一份内容搬十次也只占一份空间。"""
    store = _store(tmp_path)
    try:
        messages = [_text("同一份内容")]
        text = _fp_text(messages)
        assert asyncio.run(store.save_corpus(text, "old_forward", messages)) is True
        assert asyncio.run(store.save_corpus(text, "old_forward", messages)) is False
        assert store.corpus_stats()["rows"] == 1
    finally:
        store.close()


def test_payload_is_compressed(tmp_path):
    """大段重复文本应该被压得很小（存档不能把库撑爆）。"""
    store = _store(tmp_path)
    try:
        messages = [_text("哈哈" * 2000)]
        asyncio.run(store.save_corpus(_fp_text(messages), "old_forward", messages))
        stats = store.corpus_stats()
        assert stats["raw_bytes"] > 10000
        assert stats["packed_bytes"] < stats["raw_bytes"] / 3
    finally:
        store.close()


def test_empty_or_unusable_returns_false(tmp_path):
    store = _store(tmp_path)
    try:
        assert asyncio.run(store.save_corpus("old_forward|tsum1|x", "old_forward", [])) is False
        assert asyncio.run(store.save_corpus("", "old_forward", [_text("a")])) is False
    finally:
        store.close()


# ══════════════════════ 用存档重算 ══════════════════════
def test_rebuild_from_corpus_rewrites_hash_after_rule_change(tmp_path):
    """存档里存的是清洗前的内容 → 换规则后能重算出新哈希。"""
    from bot.services.dedup_rebuild import rebuild_from_corpus

    store = _store(tmp_path)
    try:
        raw = [_text("正文内容"), _video(), _link("https://pd.qq.com/s/abc")]
        # 模拟「老规则」入库：指纹里带着末尾链接
        old_text = build_fingerprint({}, "old_forward", forward_messages=raw, clean=False)["text"]
        asyncio.run(store.record(old_text, kind="old_forward", group_ids=["20002"]))
        asyncio.run(store.save_corpus(old_text, "old_forward", raw))
    finally:
        store.close()

    stats = rebuild_from_corpus(tmp_path / "d.db")
    assert stats["corpus"] == 1
    assert stats["updated"] == 1, "末尾链接被洗掉 → 哈希应该改写"

    conn = sqlite3.connect(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT content_hash, preview, group_ids FROM forward_fingerprint").fetchone()
    cleaned = build_fingerprint({}, "old_forward", forward_messages=raw[:2])
    assert row["content_hash"] == fingerprint_of(tokenize(cleaned["text"]))["content_hash"]
    assert "pd.qq.com" not in row["preview"]
    assert row["group_ids"] == "20002", "群标记不动"
    conn.close()


def test_rebuild_from_corpus_merges_collided_rows(tmp_path):
    """两份带不同广告的内容，洗完撞成同一哈希 → 合并成一行。"""
    from bot.services.dedup import clean_group_ids
    from bot.services.dedup_rebuild import rebuild_from_corpus

    store = _store(tmp_path)
    try:
        base = [_text("正文内容"), _video()]
        a = base + [_link("https://a.example.com/x")]
        b = base + [_link("https://b.example.com/x")]
        for msgs, groups in ((a, ["20002"]), (b, ["20003"])):
            text = build_fingerprint({}, "old_forward", forward_messages=msgs, clean=False)["text"]
            asyncio.run(store.record(text, kind="old_forward", group_ids=groups))
            asyncio.run(store.save_corpus(text, "old_forward", msgs))
    finally:
        store.close()

    stats = rebuild_from_corpus(tmp_path / "d.db")
    assert stats["merged"] == 1

    conn = sqlite3.connect(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT group_ids FROM forward_fingerprint").fetchall()
    assert len(rows) == 1
    assert clean_group_ids(rows[0]["group_ids"]) == ["20002", "20003"]
    conn.close()


def test_rebuild_from_corpus_is_idempotent(tmp_path):
    from bot.services.dedup_rebuild import rebuild_from_corpus

    store = _store(tmp_path)
    try:
        raw = [_text("正文内容"), _video()]
        text = _fp_text(raw)
        asyncio.run(store.record(text, kind="old_forward", group_ids=["20002"]))
        asyncio.run(store.save_corpus(text, "old_forward", raw))
    finally:
        store.close()

    first = rebuild_from_corpus(tmp_path / "d.db")
    second = rebuild_from_corpus(tmp_path / "d.db")
    assert first["updated"] == 0 and first["unchanged"] == 1
    assert second["updated"] == 0 and second["unchanged"] == 1


def test_rebuild_skips_kinds_that_are_not_forwarded_records(tmp_path):
    from bot.services.dedup_rebuild import rebuild_from_corpus

    store = _store(tmp_path)
    try:
        msgs = [_text("小程序")]
        text = _fp_text(msgs)
        asyncio.run(store.record(text, kind="miniapp", group_ids=["20002"]))
        asyncio.run(store.save_corpus(text, "miniapp", msgs))
    finally:
        store.close()

    stats = rebuild_from_corpus(tmp_path / "d.db")
    assert stats["skipped_kind"] == 1
    assert stats["updated"] == 0


def _video() -> dict:
    return {"sender": {"nickname": "乙"}, "message": [{"type": "video", "data": {"file": "a.mp4"}}]}
