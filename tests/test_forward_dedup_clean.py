# -*- coding: utf-8 -*-
"""入库清洗：拼指纹之前把广告 / 占位卡片去掉。

线上样本（群 1132313813 的两条聊天记录）：

* 一条记录 3 条消息，其中 2 条是嵌套聊天记录，内层各 17 条 —— 1 句「免费气泡」
  + 16 张 `com.tencent.tuwen.lua` / `bizsrc=tianxuan.shareArk` 的气泡广告，
  真正的内容只有最后那个视频；
* 另一条末尾挂着二维码海报 / 群卡片 / 纯链接。

不清洗的话，同一份内容换个人转发（广告条数一变）就会拼出两份指纹 → 反复搬运。
"""

from __future__ import annotations

import json

from bot.services.dedup import (
    build_fingerprint,
    clean_fingerprint_text,
    is_noise_body,
)
from messaging.forward_filter import clean_forward_messages, promo_card_reason

PROMO_PAYLOAD = {
    "app": "com.tencent.tuwen.lua",
    "desc": "",
    "view": "news",
    "bizsrc": "tianxuan.shareArk",
    "ver": "0.0.0.1",
    "prompt": "发现超好看的装扮",
    "meta": {
        "news": {
            "desc": "发现了一个超好看的聊天气泡",
            "jumpUrl": "https://club.vip.qq.com/transfer?open_kuikly_info=%7B%22bundle_name%22%3A%22vas_gxh_dress_detail%22%7D",
            "title": "恋爱喵兔",
            "tag": "个性装扮",
        }
    },
    "config": {"type": "normal"},
}


def _card(payload=None, *, nick="莹") -> dict:
    return {
        "sender": {"nickname": nick, "user_id": 1},
        "message": [
            {
                "type": "json",
                "data": {"data": json.dumps(payload or PROMO_PAYLOAD, ensure_ascii=False)},
            }
        ],
    }


def _text(body: str, *, nick="莹") -> dict:
    return {
        "sender": {"nickname": nick, "user_id": 1},
        "message": [{"type": "text", "data": {"text": body}}],
    }


def _video(*, nick="Rimiel") -> dict:
    return {
        "sender": {"nickname": nick, "user_id": 2},
        "message": [{"type": "video", "data": {"file": "a.mp4"}}],
    }


# ══════════════════ 装扮 / 气泡卡片 ══════════════════
def test_promo_card_is_recognised_by_bizsrc():
    assert promo_card_reason(_card()) == "promo_card"


def test_promo_card_is_recognised_without_bizsrc():
    """bizsrc 换了也认：prompt / tag / jumpUrl 任意一条命中即可。"""
    payload = {
        "app": "com.tencent.tuwen.lua",
        "prompt": "发现超好看的装扮",
        "meta": {"news": {"title": "恋爱喵兔"}},
    }
    assert promo_card_reason(_card(payload)) == "promo_card"


def test_ordinary_article_card_is_not_promo():
    payload = {
        "app": "com.tencent.tuwen.lua",
        "bizsrc": "qqconnect.sdkshare",
        "prompt": "邀请你加入幻想乡~",
        "meta": {"news": {"title": "某篇文章", "jumpUrl": "https://mp.weixin.qq.com/s/abc"}},
    }
    assert promo_card_reason(_card(payload)) is None


def test_bubble_spam_does_not_change_the_fingerprint():
    """32 张气泡广告 + 1 个视频，和「只有那个视频」必须拼出同一个指纹。"""
    noisy = (
        [_text("免费气泡")]
        + [_card() for _ in range(16)]
        + [_text("免费气泡")]
        + [_card() for _ in range(16)]
        + [_video()]
    )
    clean = [_text("免费气泡"), _video()]

    def fingerprint(messages):
        kept, _removed = clean_forward_messages(messages, image_timeout=0.01)
        return build_fingerprint({}, "old_forward", forward_messages=kept)["text"]

    assert fingerprint(noisy) == fingerprint(clean)
    assert fingerprint(noisy) == "old_forward|tsum1|Rimiel: [视频]"


def test_promo_cards_are_dropped_even_outside_the_tail():
    messages = [_text("正文"), _card(), _text("结尾", nick="B"), _video()]
    kept, removed = clean_forward_messages(messages, scan_tail=1)
    assert [item["reason"] for item in removed] == ["promo_card"]
    assert len(kept) == 3


# ══════════════════ 末尾链接 / 纯噪声行 ══════════════════
def test_noise_body_basics():
    assert is_noise_body("") is True
    assert is_noise_body("https://x.com/a") is True
    assert is_noise_body("[图片]") is False
    assert is_noise_body("这段剧情我看过 https://b23.tv/x 挺好笑的，真的非常非常好看") is False
    # 末尾几条才用宽松规则
    assert is_noise_body("这个库 https://github.com/a/b 挺好用的", loose=True) is True


def test_bare_domain_ad_in_tail_is_dropped():
    """`零食✨好物🐑毛薅不停😋↓ qm.qq.com/q/xxx` 这种没有协议的广告。"""
    messages = [_text("正文"), _text("正文2"), _text("零食✨好物🐑毛薅不停😋↓ qm.qq.com/q/VcCFoWscQU")]
    kept, removed = clean_forward_messages(messages, scan_tail=3, loose_link=True)
    assert [item["reason"] for item in removed] == ["plain_link"]
    assert len(kept) == 2


# ══════════════════ 文本级清洗（重建老库用） ══════════════════
def test_clean_fingerprint_text_drops_tail_link_and_share():
    text = (
        "old_forward|tsum5|user: 正文|user: [图片]|user: [分享]|"
        "user: 更多🔥逆天发源地↓ https://pd.qq.com/s/1|user: 结尾"
    )
    cleaned = clean_fingerprint_text(text)
    assert cleaned == "old_forward|tsum3|user: 正文|user: [图片]|user: 结尾"


def test_clean_fingerprint_text_keeps_miniapp_untouched():
    text = "miniapp|1109937557|哔哩哔哩|某个视频|pic.ugcimg.cn/abc/jpg1"
    assert clean_fingerprint_text(text) == text


def test_clean_fingerprint_text_refuses_to_over_clean():
    """只剩一句「这个真的好玩兄弟」时原样保留，别让指纹失去区分度。"""
    text = "old_forward|tsum2|user: https://h5.metaapp.cn/x/1/index.html|user: 这个真的好玩兄弟"
    assert clean_fingerprint_text(text) == text


def test_clean_fingerprint_text_keeps_media_only_record():
    text = "old_forward|tsum3|user: [视频]|user: [分享]|user: [分享]"
    cleaned = clean_fingerprint_text(text)
    assert cleaned == "old_forward|tsum1|user: [视频]"


# ══════════════════ 重建老库（--clean） ══════════════════
def _make_db(tmp_path):
    from bot.services.dedup import ForwardDedupStore

    store = ForwardDedupStore(
        db_path=tmp_path / "dedup.db", settings={"forward_dedup": {}}
    )
    conn = store._ensure_conn()
    return store, conn


def _insert(conn, text, *, groups="20002,20003", hit=1):
    from bot.services.dedup import fingerprint_of, tokenize

    fp = fingerprint_of(tokenize(text))
    conn.execute(
        "INSERT INTO forward_fingerprint (simhash, b0, b1, b2, b3, content_hash, "
        "sim_algo, kind, weak, token_cnt, preview, group_ids, first_seen, last_seen, "
        "hit_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fp["simhash"], fp["b0"], fp["b1"], fp["b2"], fp["b3"],
            fp["content_hash"], 2, "old_forward", 0,
            len(tokenize(text)), text[:120], groups, 100, 200, hit,
        ),
    )
    conn.commit()
    return fp["content_hash"]


def _write_log(path, texts):
    with open(path, "w", encoding="utf-8") as fh:
        for text in texts:
            fh.write(json.dumps({"fingerprint": {"text": text}}, ensure_ascii=False) + "\n")


def test_rebuild_cleaned_rewrites_content_hash(tmp_path):
    from bot.services.dedup import fingerprint_of, tokenize
    from bot.services.dedup_rebuild import rebuild_cleaned

    store, conn = _make_db(tmp_path)
    try:
        old = (
            "old_forward|tsum4|user: 正文|user: [图片]|"
            "user: 更多🔥逆天发源地↓ https://pd.qq.com/s/1|user: 结尾"
        )
        old_hash = _insert(conn, old)
        log = tmp_path / "log.jsonl"
        _write_log(log, [old])

        stats = rebuild_cleaned(tmp_path / "dedup.db", log)
        assert stats["updated"] == 1 and stats["merged"] == 0

        new = "old_forward|tsum3|user: 正文|user: [图片]|user: 结尾"
        new_hash = fingerprint_of(tokenize(new))["content_hash"]
        row = conn.execute(
            "SELECT content_hash, preview FROM forward_fingerprint"
        ).fetchone()
        assert row["content_hash"] == new_hash
        assert row["content_hash"] != old_hash
        assert row["preview"] == new
    finally:
        store.close()


def test_rebuild_cleaned_merges_rows_that_collapse_together(tmp_path):
    from bot.services.dedup import clean_group_ids
    from bot.services.dedup_rebuild import rebuild_cleaned

    store, conn = _make_db(tmp_path)
    try:
        base = "old_forward|tsum3|user: 正文|user: [图片]|user: 结尾"
        noisy_a = base.replace("|user: 结尾", "|user: https://a.example.com/x|user: 结尾")
        noisy_b = base.replace("|user: 结尾", "|user: https://b.example.com/x|user: 结尾")
        _insert(conn, noisy_a, groups="20002", hit=2)
        _insert(conn, noisy_b, groups="20003", hit=5)

        log = tmp_path / "log.jsonl"
        _write_log(log, [noisy_a, noisy_b])

        stats = rebuild_cleaned(tmp_path / "dedup.db", log)
        assert stats["merged"] == 1
        assert stats["updated"] == 1

        rows = conn.execute(
            "SELECT group_ids, hit_count FROM forward_fingerprint"
        ).fetchall()
        assert len(rows) == 1, "同一份内容清洗后撞在一起，应该只剩一行"
        assert clean_group_ids(rows[0]["group_ids"]) == ["20002", "20003"]
        assert rows[0]["hit_count"] == 5, "命中次数取大，不累加"
    finally:
        store.close()


def test_gate_fingerprints_cleaned_content(tmp_path, monkeypatch):
    """闸门里也要洗：气泡卡片不该出现在指纹文本里。"""
    import asyncio
    from types import SimpleNamespace

    import bot.services.dedup as dedup_module
    from bot.services.forward import ForwardService

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    store, _conn = _make_db(tmp_path)
    svc = ForwardService(
        mp=SimpleNamespace(),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    noisy = [_text("免费气泡")] + [_card() for _ in range(4)] + [_video()]

    async def fake_expand(event, kind="old_forward"):
        return noisy, "test"

    svc._forward_kind = lambda event, miniapp_info=None: "old_forward"
    svc._expand_forward_for_dedup = fake_expand
    svc._fetch_forward_video_hashes = lambda *a, **k: _empty_videos()
    svc._fetch_forward_image_hashes = lambda *a, **k: _empty_images()

    try:
        event = {
            "message_type": "group",
            "group_id": 20003,
            "message_id": 1,
            "message": [{"type": "forward", "data": {"id": "fwd"}}],
        }
        outcome = asyncio.run(
            svc.forward_dedup_gate(event, target_groupid=[20004])
        )
        assert "[分享]" not in outcome.fingerprint["text"]
        assert outcome.fingerprint["text"] == "old_forward|tsum1|Rimiel: [视频]"
    finally:
        store.close()


async def _empty_videos(*_args, **_kwargs):
    return [], {"url_cnt": 0}


async def _empty_images(*_args, **_kwargs):
    return [], {"url_cnt": 0}


# ══════════════════ 清洗前的老指纹（别名查询） ══════════════════
# 清洗是后加的：它之前入库的行存的是「带广告」的哈希。只按清洗后的文本查，
# 这些老行永远匹配不上 → 同一份内容每重发一次就搬一次。
RAW_LEGACY = (
    "old_forward|tsum3|user: 正文|user: [图片]|"
    "user: 更多🔥逆天发源地↓ https://pd.qq.com/s/1"
)
CLEANED = "old_forward|tsum2|user: 正文|user: [图片]"


def test_build_fingerprint_can_reproduce_pre_clean_text():
    """clean=False 要能拼回「清洗之前」的文本，别名查询才有东西可查。"""
    messages = [_text("正文"), _video(), _text("更多🔥逆天发源地↓ https://pd.qq.com/s/1")]
    clean_text = build_fingerprint({}, "old_forward", forward_messages=messages)["text"]
    raw_text = build_fingerprint(
        {}, "old_forward", forward_messages=messages, clean=False
    )["text"]
    assert "pd.qq.com" not in clean_text
    assert "pd.qq.com" in raw_text


def test_find_hits_legacy_hash_via_alt_text(tmp_path):
    import asyncio

    from bot.services.dedup import ForwardDedupStore

    store = ForwardDedupStore(db_path=tmp_path / "d.db", settings={"forward_dedup": {}})
    try:
        # 清洗上线之前入库：存的是带广告的整串
        asyncio.run(
            store.record(RAW_LEGACY, kind="old_forward", group_ids=["20002", "20003"])
        )
        # 只按清洗后的文本查 → 对不上（这就是老行被反复重搬的原因）
        assert asyncio.run(store.find(CLEANED, kind="old_forward", group_ids=["20002"])) is None

        hit = asyncio.run(
            store.find(
                CLEANED,
                kind="old_forward",
                group_ids=["20002"],
                cur_group="20003",
                alt_text=RAW_LEGACY,
            )
        )
        assert hit is not None and hit["reason"] == "content_hash_legacy"
        assert hit["seen_here"] is True

        # 命中时会把那一行就地升级 → 之后只给清洗后的文本也能直接命中
        again = asyncio.run(
            store.find(CLEANED, kind="old_forward", group_ids=["20002"], cur_group="20003")
        )
        assert again is not None and again["reason"] == "content_hash"
        assert again["id"] == hit["id"], "升级是同一行，不是新建一条"
    finally:
        store.close()


def test_promote_keeps_row_identity_and_hit_count(tmp_path):
    import asyncio
    import sqlite3

    from bot.services.dedup import ForwardDedupStore

    store = ForwardDedupStore(db_path=tmp_path / "d.db", settings={"forward_dedup": {}})
    try:
        asyncio.run(
            store.record(RAW_LEGACY, kind="old_forward", group_ids=["20002"])
        )
        conn = sqlite3.connect(tmp_path / "d.db")
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM forward_fingerprint").fetchone())

        asyncio.run(
            store.find(CLEANED, kind="old_forward", group_ids=["20002"], alt_text=RAW_LEGACY)
        )

        after = dict(conn.execute("SELECT * FROM forward_fingerprint").fetchone())
        conn.close()
        assert after["id"] == before["id"]
        assert after["content_hash"] != before["content_hash"]
        assert after["preview"] == CLEANED, "预览跟着换成清洗后的文本"
        assert after["group_ids"] == before["group_ids"], "群标记不动"
    finally:
        store.close()


def test_gate_passes_pre_clean_alias_only_when_cleaning_removed_something(
    tmp_path, monkeypatch
):
    """清洗真的动过内容时才查别名；没动过就省一次查询。"""
    import asyncio
    from types import SimpleNamespace

    import bot.services.dedup as dedup_module
    from bot.services.forward import ForwardService

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    store = dedup_module.ForwardDedupStore(
        db_path=tmp_path / "d.db", settings={"forward_dedup": {}}
    )
    svc = ForwardService(
        mp=SimpleNamespace(),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    captured: dict = {}

    async def fake_find(text, weak=False, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return None

    monkeypatch.setattr(store, "find", fake_find)
    svc._forward_kind = lambda event, miniapp_info=None: "old_forward"
    svc._fetch_forward_image_hashes = lambda *a, **k: _empty_images()
    svc._fetch_forward_video_hashes = lambda *a, **k: _empty_videos()

    def run(messages):
        captured.clear()

        async def fake_expand(event, kind="old_forward"):
            return messages, "test"

        svc._expand_forward_for_dedup = fake_expand
        event = {
            "message_type": "group",
            "group_id": 20003,
            "message_id": 1,
            "message": [{"type": "forward", "data": {"id": "fwd"}}],
        }
        return asyncio.run(svc.forward_dedup_gate(event, target_groupid=[20004]))

    try:
        # 有广告 → 清洗掉 → 带别名
        run([_text("正文"), _video(), _text("更多🔥逆天发源地↓ https://pd.qq.com/s/1")])
        assert "pd.qq.com" not in captured["text"]
        assert "pd.qq.com" in captured["alt_text"]

        # 没有广告 → 清洗什么都没动 → 不查别名
        run([_text("正文"), _video()])
        assert captured["alt_text"] == ""
    finally:
        store.close()


# ══════════════════ 清洗前的老指纹（别名查询） ══════════════════
# 清洗是后加的：它之前入库的行存的是「带广告」的哈希。只按清洗后的文本查，
# 这些老行永远匹配不上 → 同一份内容每重发一次就搬一次。
RAW_LEGACY = (
    "old_forward|tsum3|user: 正文|user: [图片]|"
    "user: 更多🔥逆天发源地↓ https://pd.qq.com/s/1"
)
CLEANED = "old_forward|tsum2|user: 正文|user: [图片]"


def test_build_fingerprint_can_reproduce_pre_clean_text():
    """clean=False 要能拼回「清洗之前」的文本，别名查询才有东西可查。"""
    messages = [_text("正文"), _video(), _text("更多🔥逆天发源地↓ https://pd.qq.com/s/1")]
    clean_text = build_fingerprint({}, "old_forward", forward_messages=messages)["text"]
    raw_text = build_fingerprint(
        {}, "old_forward", forward_messages=messages, clean=False
    )["text"]
    assert "pd.qq.com" not in clean_text
    assert "pd.qq.com" in raw_text


def test_find_hits_legacy_hash_via_alt_text(tmp_path):
    import asyncio

    from bot.services.dedup import ForwardDedupStore

    store = ForwardDedupStore(db_path=tmp_path / "d.db", settings={"forward_dedup": {}})
    try:
        # 清洗上线之前入库：存的是带广告的整串
        asyncio.run(
            store.record(RAW_LEGACY, kind="old_forward", group_ids=["20002", "20003"])
        )
        # 只按清洗后的文本查 → 对不上（这就是老行被反复重搬的原因）
        assert asyncio.run(store.find(CLEANED, kind="old_forward", group_ids=["20002"])) is None

        hit = asyncio.run(
            store.find(
                CLEANED,
                kind="old_forward",
                group_ids=["20002"],
                cur_group="20003",
                alt_text=RAW_LEGACY,
            )
        )
        assert hit is not None and hit["reason"] == "content_hash_legacy"
        assert hit["seen_here"] is True

        # 命中时会把那一行就地升级 → 之后只给清洗后的文本也能直接命中
        again = asyncio.run(
            store.find(CLEANED, kind="old_forward", group_ids=["20002"], cur_group="20003")
        )
        assert again is not None and again["reason"] == "content_hash"
        assert again["id"] == hit["id"], "升级是同一行，不是新建一条"
    finally:
        store.close()


def test_promote_keeps_row_identity_and_hit_count(tmp_path):
    import asyncio
    import sqlite3

    from bot.services.dedup import ForwardDedupStore

    store = ForwardDedupStore(db_path=tmp_path / "d.db", settings={"forward_dedup": {}})
    try:
        asyncio.run(
            store.record(RAW_LEGACY, kind="old_forward", group_ids=["20002"])
        )
        conn = sqlite3.connect(tmp_path / "d.db")
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM forward_fingerprint").fetchone())

        asyncio.run(
            store.find(CLEANED, kind="old_forward", group_ids=["20002"], alt_text=RAW_LEGACY)
        )

        after = dict(conn.execute("SELECT * FROM forward_fingerprint").fetchone())
        conn.close()
        assert after["id"] == before["id"]
        assert after["content_hash"] != before["content_hash"]
        assert after["preview"] == CLEANED, "预览跟着换成清洗后的文本"
        assert after["group_ids"] == before["group_ids"], "群标记不动"
    finally:
        store.close()


def test_gate_passes_pre_clean_alias_only_when_cleaning_removed_something(
    tmp_path, monkeypatch
):
    """清洗真的动过内容时才查别名；没动过就省一次查询。"""
    import asyncio
    from types import SimpleNamespace

    import bot.services.dedup as dedup_module
    from bot.services.forward import ForwardService

    monkeypatch.setattr(dedup_module, "LOG_ENABLED", False)
    store = dedup_module.ForwardDedupStore(
        db_path=tmp_path / "d.db", settings={"forward_dedup": {}}
    )
    svc = ForwardService(
        mp=SimpleNamespace(),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    captured: dict = {}

    async def fake_find(text, weak=False, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return None

    monkeypatch.setattr(store, "find", fake_find)
    svc._forward_kind = lambda event, miniapp_info=None: "old_forward"
    svc._fetch_forward_image_hashes = lambda *a, **k: _empty_images()
    svc._fetch_forward_video_hashes = lambda *a, **k: _empty_videos()

    def run(messages):
        captured.clear()

        async def fake_expand(event, kind="old_forward"):
            return messages, "test"

        svc._expand_forward_for_dedup = fake_expand
        event = {
            "message_type": "group",
            "group_id": 20003,
            "message_id": 1,
            "message": [{"type": "forward", "data": {"id": "fwd"}}],
        }
        return asyncio.run(svc.forward_dedup_gate(event, target_groupid=[20004]))

    try:
        # 有广告 → 清洗掉 → 带别名
        run([_text("正文"), _video(), _text("更多🔥逆天发源地↓ https://pd.qq.com/s/1")])
        assert "pd.qq.com" not in captured["text"]
        assert "pd.qq.com" in captured["alt_text"]

        # 没有广告 → 清洗什么都没动 → 不查别名
        run([_text("正文"), _video()])
        assert captured["alt_text"] == ""
    finally:
        store.close()
