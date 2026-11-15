# -*- coding: utf-8 -*-
"""历史入库（`bot.services.dedup_ingest`）：只登记、不转发。

夹具全是离线的：假内核按 action 返回预设结果，不碰网络、不碰真实数据库。
"""

from __future__ import annotations

import asyncio
import json

from AbstractionLayer.kernal import CallableTransport, OneBotKernel, SNOWLUMA
from bot.services.dedup import ForwardDedupStore, fingerprint_of, tokenize
from bot.services.dedup_ingest import HistoryIngestor, delete_legacy_rows
from bot.services.forward import ForwardService

TARGETS = (1079845768, 1054955587, 933109193)
BOT_UIN = 3122793026

CARD_CONTENT = [
    {
        "sender": {"nickname": "甲", "user_id": 1},
        "message": [
            {"type": "text", "data": {"text": "今天这件事真的很离谱我到现在还想不通为什么会这样"}}
        ],
    },
    {
        "sender": {"nickname": "乙", "user_id": 2},
        "message": [
            {"type": "text", "data": {"text": "别说了我已经笑到不行了怎么会有这种人啊"}}
        ],
    },
]


def _card(message_id: int, forward_id: str, *, when: int = 1793000000, user_id: int = 111):
    return {
        "message_id": message_id,
        "real_id": message_id,
        "message_seq": message_id,
        "message_type": "group",
        "group_id": 1079845768,
        "user_id": user_id,
        "time": when,
        "sender": {"user_id": user_id, "nickname": "搬运工", "card": ""},
        "raw_message": f"[CQ:forward,id={forward_id}]",
        "message": [{"type": "forward", "data": {"id": forward_id}}],
    }


def _history_page(messages):
    return {"status": "ok", "retcode": 0, "data": {"messages": messages}}


def _forward_reply(forward_id: str = ""):
    """假内容：多加一句带 id 的正文，让不同的卡拼出不同的指纹。"""
    extra = {
        "sender": {"nickname": "丙", "user_id": 3},
        "message": [{"type": "text", "data": {"text": f"补充说明 {forward_id}"}}],
    }
    return {"status": "ok", "retcode": 0, "data": {"messages": CARD_CONTENT + [extra]}}


def _make(monkeypatch, tmp_path, pages, *, dry_run=False):
    """假内核 + 真 ForwardService（假 mp / 假 settings），库用临时文件。"""
    calls: list[tuple[str, dict]] = []

    async def transport(action: str, params: dict) -> dict:
        calls.append((action, params))
        if action == "get_group_msg_history":
            index = params.get("message_id")
            page = pages.get("first") if index is None else pages.get(str(index))
            return _history_page(page or [])
        if action == "get_forward_msg":
            return _forward_reply(str(params.get("message_id") or ""))
        return {"status": "ok", "retcode": 0, "data": {}}

    kernel = OneBotKernel(
        CallableTransport(transport, label="fake"), capabilities=SNOWLUMA
    )
    store = ForwardDedupStore(
        db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}}
    )
    service = ForwardService(
        mp=__import__("types").SimpleNamespace(),
        api=__import__("types").SimpleNamespace(kernel=kernel),
        settings=__import__("types").SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    ingestor = HistoryIngestor(
        kernel=kernel,
        store=store,
        service=service,
        targets=TARGETS,
        bot_uin=BOT_UIN,
        dry_run=dry_run,
    )
    return ingestor, store, calls


def test_scan_group_records_forward_cards(monkeypatch, tmp_path):
    pages = {"first": [_card(1, "F1", when=1793000000), _card(2, "F2", when=1792999000)]}
    ingestor, store, calls = _make(monkeypatch, tmp_path, pages)
    try:
        summary = asyncio.run(ingestor.scan_group(1079845768, days=60))
        assert summary["pages"] == 1

        conn = store._ensure_conn()
        rows = conn.execute(
            "SELECT kind, preview, group_ids, first_seen FROM forward_fingerprint"
        ).fetchall()
        assert len(rows) == 2, "两张卡各入一行"
        assert {row["kind"] for row in rows} == {"old_forward"}
        assert all(row["group_ids"] == ",".join(str(g) for g in TARGETS) for row in rows)
        assert {row["first_seen"] for row in rows} == {1793000000, 1792999000}, (
            "用消息自己的时间，不是导入时刻"
        )

        stats = ingestor.stats
        assert stats["messages"] == 2 and stats["cards"] == 2
        assert stats["recorded"] == 2
        assert [c[0] for c in calls].count("get_forward_msg") == 2
    finally:
        store.close()


def test_same_forward_id_is_expanded_once(monkeypatch, tmp_path):
    pages = {
        "first": [_card(1, "F1"), _card(2, "F1"), _card(3, "F1")],
    }
    ingestor, store, calls = _make(monkeypatch, tmp_path, pages)
    try:
        asyncio.run(ingestor.scan_group(1079845768, days=60))
        assert [c[0] for c in calls].count("get_forward_msg") == 1
        assert ingestor.stats["dup_card"] == 2
        assert ingestor.stats["recorded"] == 1
    finally:
        store.close()


def test_bot_own_cards_are_skipped(monkeypatch, tmp_path):
    pages = {
        "first": [_card(1, "F1", user_id=BOT_UIN), _card(2, "F2")],
    }
    ingestor, store, _calls = _make(monkeypatch, tmp_path, pages)
    try:
        asyncio.run(ingestor.scan_group(1079845768, days=60))
        assert ingestor.stats["self_skip"] == 1
        assert ingestor.stats["recorded"] == 1
    finally:
        store.close()


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    pages = {"first": [_card(1, "F1")]}
    ingestor, store, _calls = _make(monkeypatch, tmp_path, pages, dry_run=True)
    try:
        asyncio.run(ingestor.scan_group(1079845768, days=60))
        conn = store._ensure_conn()
        assert conn.execute("SELECT COUNT(*) FROM forward_fingerprint").fetchone()[0] == 0
        assert ingestor.stats["recorded"] == 1, "演练也计数，方便估算工作量"
    finally:
        store.close()


def test_oldest_timestamp_stops_paging(monkeypatch, tmp_path):
    """翻到早于 days 天的那一页就停，不再往后翻。"""
    old = 1793000000
    pages = {
        "first": [_card(10, "F10", when=old), _card(11, "F11", when=old)],
        str(10): [_card(8, "F8", when=old - 90 * 86400)],
    }
    ingestor, store, calls = _make(monkeypatch, tmp_path, pages)
    try:
        asyncio.run(ingestor.scan_group(1079845768, days=60))
        history_calls = [c for c in calls if c[0] == "get_group_msg_history"]
        assert len(history_calls) == 2, "第二页已经越过 60 天，不该再翻第三页"
        assert history_calls[1][1]["message_id"] == 10, "游标是最旧那条的 message_id"
        assert history_calls[1][1]["reverse_order"] is True
        assert history_calls[1][1]["count"] <= SNOWLUMA.history_max_count
    finally:
        store.close()


# ══════════════════ 老行清理 ══════════════════
def _insert(conn, kind: str, text: str):
    fp = fingerprint_of(tokenize(text))
    conn.execute(
        "INSERT INTO forward_fingerprint (simhash, b0, b1, b2, b3, content_hash, "
        "sim_algo, kind, weak, token_cnt, preview, group_ids, first_seen, last_seen, "
        "hit_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fp["simhash"], fp["b0"], fp["b1"], fp["b2"], fp["b3"],
            fp["content_hash"], 2, kind, 0, fp["token_cnt"], text[:120],
            "20002", 100, 200, 0,
        ),
    )
    conn.commit()
    return fp["content_hash"]


def test_delete_legacy_only_touches_rows_without_text(tmp_path):
    store = ForwardDedupStore(db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}})
    try:
        conn = store._ensure_conn()
        keep_hash = _insert(conn, "old_forward", "old_forward|tsum2|user: 甲: 有原文")
        _insert(conn, "old_forward", "old_forward|tsum2|user: 乙: 没原文")
        _insert(conn, "miniapp", "miniapp|1109937557|哔哩哔哩|标题|图")

        outcome = delete_legacy_rows(store, {keep_hash})
        assert outcome == {"candidates": 1, "deleted": 1, "dry_run": False}

        left = conn.execute("SELECT kind, preview FROM forward_fingerprint").fetchall()
        assert len(left) == 2
        assert {row["kind"] for row in left} == {"old_forward", "miniapp"}
        assert any("有原文" in row["preview"] for row in left)
    finally:
        store.close()


def test_delete_legacy_dry_run_keeps_rows(tmp_path):
    store = ForwardDedupStore(db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}})
    try:
        _insert(store._ensure_conn(), "old_forward", "old_forward|tsum1|user: 甲: 没原文")
        outcome = delete_legacy_rows(store, set(), dry_run=True)
        assert outcome["candidates"] == 1 and outcome["deleted"] == 0
        assert store._ensure_conn().execute(
            "SELECT COUNT(*) FROM forward_fingerprint"
        ).fetchone()[0] == 1
    finally:
        store.close()
