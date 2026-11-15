# -*- coding: utf-8 -*-
"""视频指纹在去重库里的匹配行为（不经过 ForwardService）。"""
from __future__ import annotations

from bot.services.dedup import ForwardDedupStore


def _store(tmp_path) -> ForwardDedupStore:
    return ForwardDedupStore(
        db_path=tmp_path / "dedup.db",
        settings={"forward_dedup": {}},
    )


def _video(sig_char: str = "a", byte_char: str | None = "b", *, size: int = 123) -> dict:
    sig = sig_char if len(sig_char) == 64 else sig_char * 64
    return {
        "sig": sig,
        "byte_hash": (byte_char * 64) if byte_char else "",
        "bytes": size,
    }


def _flip_one_bit(sig: str) -> str:
    # 只翻最后一个 hex 位（最多 4 bit），模拟转码后帧指纹差一点
    last = "0" if sig[-1] != "0" else "1"
    return sig[:-1] + last


def test_video_only_weak_matches_without_images(tmp_path):
    store = _store(tmp_path)
    text = "user|视频记录|今天|天气|不错"
    videos = [_video()]
    assert store._record_sync(text, weak=True, videos=videos) > 0

    matched = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=videos,
        image_required=False,
        video_required=True,
    )
    assert matched is not None
    assert matched["reason"] == "content_hash"


def test_video_only_weak_rejects_different_video(tmp_path):
    store = _store(tmp_path)
    text = "user|视频记录|今天|天气|不错"
    store._record_sync(text, weak=True, videos=[_video("a", "b")])

    matched = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=[_video("c", "d")],
        image_required=False,
        video_required=True,
    )
    assert matched is None


def test_video_near_duplicate_uses_hamming(tmp_path):
    store = _store(tmp_path)
    text = "user|视频记录|今天|天气|不错"
    original = _video("a", "b")
    store._record_sync(text, weak=True, videos=[original])

    # 字节哈希不同、帧指纹只差 1 位 → 仍应命中
    near = _video(_flip_one_bit("a" * 64), "d")
    matched = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=[near],
        image_required=False,
        video_required=True,
    )
    assert matched is not None


def test_video_required_fails_when_hash_missing(tmp_path):
    store = _store(tmp_path)
    text = "user|视频记录|今天|天气|不错"
    store._record_sync(text, weak=True, videos=[_video()])

    matched = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=[],
        image_required=False,
        video_required=True,
    )
    assert matched is None


def test_video_fallback_when_text_drifts(tmp_path):
    store = _store(tmp_path)
    videos = [_video()]
    store._record_sync("user|旧标题|第一条|第二条|第三条", weak=True, videos=videos)

    matched = store._find_sync(
        "user|新标题|完全|不同|措辞",
        weak=True,
        images=[],
        videos=videos,
        image_required=False,
        video_required=True,
    )
    assert matched is not None
    assert matched["reason"] == "video"


def test_multiple_videos_need_match_ratio(tmp_path):
    store = _store(tmp_path)
    text = "user|视频合集|第一条|第二条|第三条"
    a, b = _video("a", "1"), _video("b", "2")
    store._record_sync(text, weak=True, videos=[a, b])

    # 只对上 1/2 = 0.5 < 0.6，不算
    half = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=[a, _video("c", "3")],
        image_required=False,
        video_required=True,
    )
    assert half is None

    # 2/2 = 1.0，算
    full = store._find_sync(
        text,
        weak=True,
        images=[],
        videos=[a, b],
        image_required=False,
        video_required=True,
    )
    assert full is not None
