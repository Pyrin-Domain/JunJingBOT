# -*- coding: utf-8 -*-
"""图片送达库：独立于聊天记录指纹库的第二个库。

要点：
1. 粒度是 (消息身份, 会话)，不是「内容」；
2. 判定 = 这次任意一个身份 + 目标会话命中过任意一行；
3. 落盘（重启后仍然记得），过期能清。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from bot.services import dedup as dedup_module
from bot.services.image_dedup import ForwardImageStore, chat_key


def _store(tmp_path, **overrides) -> ForwardImageStore:
    return ForwardImageStore(
        db_path=tmp_path / "forward_image.db",
        settings={"forward_image_dedup": overrides},
    )


def test_chat_key_separates_group_and_private():
    assert chat_key(group_id=20002) == "g:20002"
    assert chat_key(user_id=10001) == "u:10001"
    assert chat_key() == ""


def test_remember_then_delivered(tmp_path):
    store = _store(tmp_path)
    try:
        identities = ["real_id:777", "message_id:20002:333"]
        chats = ["g:20002", "g:20003"]
        assert store.delivered(identities, chats) == set()

        store.remember(identities, chats)
        assert store.delivered(identities, ["g:20002"]) == {"g:20002"}
        # 任意一个身份命中就算送达（实时事件没有 real_id，回放只有 real_id）
        assert store.delivered(["real_id:777"], ["g:20003"]) == {"g:20003"}
        # 没送过的会话不算
        assert store.delivered(identities, ["g:99999"]) == set()
    finally:
        store.close()


def test_async_wrappers_run_sqlite_off_event_loop_thread(tmp_path):
    store = _store(tmp_path)
    main_thread = threading.get_ident()
    delivered_threads: list[int] = []
    remember_threads: list[int] = []

    original_delivered = store.delivered
    original_remember = store.remember

    def tracked_delivered(identities, chats):
        delivered_threads.append(threading.get_ident())
        return original_delivered(identities, chats)

    def tracked_remember(identities, chats, now=None):
        remember_threads.append(threading.get_ident())
        return original_remember(identities, chats, now)

    store.delivered = tracked_delivered
    store.remember = tracked_remember
    identities = ["real_id:901"]
    chats = ["g:20002"]

    async def exercise():
        assert await store.delivered_async(identities, chats) == set()
        await store.remember_async(identities, chats)
        assert await store.delivered_async(identities, chats) == set(chats)

    try:
        asyncio.run(exercise())
        assert delivered_threads and remember_threads
        assert main_thread not in delivered_threads
        assert main_thread not in remember_threads
    finally:
        store.close()


def test_async_wrappers_serialize_concurrent_writes(tmp_path):
    store = _store(tmp_path)
    pairs = [(f"real_id:{i}", f"g:{i}") for i in range(20)]

    async def remember_all():
        await asyncio.gather(
            *(store.remember_async([identity], [chat]) for identity, chat in pairs)
        )

    try:
        asyncio.run(remember_all())
        assert store.count() == len(pairs)
        for identity, chat in pairs:
            assert store.delivered([identity], [chat]) == {chat}
    finally:
        store.close()


def test_unknown_identity_never_matches(tmp_path):
    store = _store(tmp_path)
    try:
        store.remember(["real_id:1"], ["g:20002"])
        assert store.delivered([], ["g:20002"]) == set()
        assert store.delivered(["real_id:1"], []) == set()
    finally:
        store.close()


def test_store_survives_restart(tmp_path):
    store = _store(tmp_path)
    store.remember(["real_id:777"], ["g:20002"])
    store.close()

    reopened = _store(tmp_path)
    try:
        assert reopened.delivered(["real_id:777"], ["g:20002"]) == {"g:20002"}
    finally:
        reopened.close()


def test_expired_rows_are_pruned(tmp_path):
    store = _store(tmp_path, keep_days=1)
    try:
        store.remember(["real_id:1"], ["g:1"], now=int(time.time()) - 3 * 86400)
        store.remember(["real_id:2"], ["g:2"])
        store._last_prune = 0.0
        store._maybe_prune()

        assert store.delivered(["real_id:1"], ["g:1"]) == set()
        assert store.delivered(["real_id:2"], ["g:2"]) == {"g:2"}
    finally:
        store.close()


def test_db_path_config_is_used_with_settings(tmp_path):
    """配置里的 db_path 要真正生效（相对路径落到 data/ 下）。"""

    class Settings:
        paths = SimpleNamespace(data_file=lambda name: tmp_path / name)

        def section(self, name):
            assert name == "forward_image_dedup"
            return {"db_path": "custom_image.db"}

    store = ForwardImageStore(settings=Settings())
    try:
        store._ensure_conn()
        assert store.db_path == tmp_path / "custom_image.db"
    finally:
        store.close()


def test_image_db_is_a_separate_file(tmp_path):
    """图片库和聊天记录指纹库必须是两个文件，别混。"""
    store = _store(tmp_path)
    try:
        store._ensure_conn()
        assert store.db_path.name == "forward_image.db"
        assert store.db_path != dedup_module.DEFAULT_DB_PATH
    finally:
        store.close()


def test_tables_do_not_collide_with_fingerprint_db(tmp_path):
    store = _store(tmp_path)
    try:
        conn = store._ensure_conn()
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "forward_image_delivery" in names
        assert "forward_fingerprint" not in names
    finally:
        store.close()
