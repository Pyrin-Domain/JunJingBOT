# -*- coding: utf-8 -*-
"""转发去重库里那些不经过公开 API 的边界行为（图片兜底、旧库兼容）。"""
import asyncio
import time

from bot.services.dedup import ForwardDedupStore, fingerprint_of, tokenize


def _store(tmp_path) -> ForwardDedupStore:
    # 传一个空的 forward_dedup 段，避免测试读真实 config.json。
    return ForwardDedupStore(
        db_path=tmp_path / "dedup.db",
        settings={"forward_dedup": {}},
    )


def _insert_row(store, *, simhash, content_hash, images, groups):
    conn = store._ensure_conn()
    cursor = conn.execute(
        "INSERT INTO forward_fingerprint ("
        "simhash, b0, b1, b2, b3, content_hash, sim_algo, kind, weak, token_cnt, "
        "preview, group_ids, first_seen, last_seen, hit_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            simhash,
            simhash[0:4],
            simhash[4:8],
            simhash[8:12],
            simhash[12:16],
            content_hash,
            2,
            "old_forward",
            1,
            len(tokenize("old_forward|tsum5|user: 动物院有被震惊到")),
            "old_forward|tsum5|user: 动物院有被震惊到",
            ",".join(groups),
            int(time.time()) - 100,
            int(time.time()) - 100,
            0,
        ),
    )
    fp_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO forward_image (hash, fp_id, group_ids, first_seen) "
        "VALUES (?,?,?,?)",
        [(img, fp_id, ",".join(groups), int(time.time()) - 100) for img in images],
    )
    conn.commit()
    return fp_id


def test_image_fallback_matches_near_duplicate_hash(tmp_path):
    """同一张图被转码后 dHash 差 1 位：精确 IN 查不到，模糊扫描必须能兜住。"""
    store = _store(tmp_path)
    try:
        new_text = (
            "old_forward|tsum5|user: 动物院有被震惊到|user: [图片]|"
            "user: [图片]|user: [图片]"
        )
        new_fp = fingerprint_of(tokenize(new_text))
        # 故意让旧行的 SimHash 分桶全部对不上，确保只能走图片兜底。
        old_bands = [
            "ffff" if new_fp[f"b{i}"] != "ffff" else "0000" for i in range(4)
        ]
        old_id = _insert_row(
            store,
            simhash="".join(old_bands),
            content_hash="0" * 16,
            images=["e0f8fcfc3e3ef2f086a29488e4648486"],
            groups=["20002", "20003"],
        )

        result = asyncio.run(
            store.find(
                new_text,
                weak=True,
                images=["e0f8fcfc3e3ef2f086a29488e4648686"],
                group_ids=["20002", "20004"],
                cur_group="20002",
            )
        )

        assert result is not None
        assert result["id"] == old_id
        assert result["reason"] == "image"
        assert result["seen_here"] is True
    finally:
        store.close()


def test_image_fallback_rejects_distant_hashes(tmp_path):
    """模糊扫描仍要遵守 20 bit 阈值，不能把不相干的图当成同一张。"""
    store = _store(tmp_path)
    try:
        new_text = (
            "old_forward|tsum5|user: 动物院有被震惊到|user: [图片]|"
            "user: [图片]|user: [图片]"
        )
        new_fp = fingerprint_of(tokenize(new_text))
        old_bands = [
            "ffff" if new_fp[f"b{i}"] != "ffff" else "0000" for i in range(4)
        ]
        _insert_row(
            store,
            simhash="".join(old_bands),
            content_hash="1" * 16,
            images=["0" * 32],
            groups=["20002"],
        )

        result = asyncio.run(
            store.find(
                new_text,
                weak=True,
                images=["f" * 32],
                group_ids=["20002"],
                cur_group="20002",
            )
        )

        assert result is None
    finally:
        store.close()
