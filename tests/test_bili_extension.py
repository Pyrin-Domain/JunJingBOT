"""``Extension.bilibilidownloadtool.api`` 的纯逻辑测试（不联网）。

覆盖桥接层里最容易写错、又不需要真的连 B 站的三块：

1. 上游 ``qualities`` 有 2 元组 / 3 元组两种形状，归一化必须都吃得下；
2. 画质解析 ``auto`` / 名字 / 数字 + ``max_qn`` 封顶的取舍；
3. 码流选择：指定编码 → AVC → HEVC → AV1 的回退顺序。

以及 provider 侧的文件名清洗与「引擎不可用只记一次」的行为。
"""

from __future__ import annotations

import pytest

from Extension.bilibilidownloadtool import api
from download_system.providers.bilibili import BilibiliProvider


# ── qualities 归一化 ────────────────────────────────────────────
def test_normalize_qualities_accepts_three_tuple():
    rows = api.normalize_qualities([(80, 7, "1080P"), (64, 12, "720P")])
    assert [(r.qn, r.codecid, r.name) for r in rows] == [
        (80, 7, "1080P"),
        (64, 12, "720P"),
    ]


def test_normalize_qualities_accepts_legacy_two_tuple():
    rows = api.normalize_qualities([(80, "1080P"), (64, "720P")])
    assert [(r.qn, r.codecid) for r in rows] == [(80, 0), (64, 0)]


def test_normalize_qualities_sorts_by_qn_desc_then_codec():
    rows = api.normalize_qualities([(64, 12, "720P"), (80, 12, "1080P"), (80, 7, "1080P")])
    assert [(r.qn, r.codecid) for r in rows] == [(80, 7), (80, 12), (64, 12)]


def test_normalize_qualities_skips_garbage():
    rows = api.normalize_qualities([None, ("x", "y"), (32, 7, "480P"), []])
    assert [r.qn for r in rows] == [32]


# ── 画质解析 ────────────────────────────────────────────────────
QUALITIES = api.normalize_qualities(
    [(80, 7, "1080P"), (64, 7, "720P"), (32, 7, "480P"), (16, 7, "360P")]
)


def test_resolve_qn_auto_picks_highest():
    assert api.resolve_qn(QUALITIES) == 80


def test_resolve_qn_auto_respects_max_qn():
    assert api.resolve_qn(QUALITIES, "auto", max_qn=64) == 64


def test_resolve_qn_accepts_number_and_name():
    assert api.resolve_qn(QUALITIES, 32) == 32
    assert api.resolve_qn(QUALITIES, "720p") == 64


def test_resolve_qn_falls_back_to_lower_quality():
    # 视频只有 720P，用户要 1080P：给最接近的更低画质，而不是报错
    assert api.resolve_qn(QUALITIES, "1080p") == 80
    low_only = api.normalize_qualities([(32, 7, "480P")])
    assert api.resolve_qn(low_only, "1080p") == 32


def test_resolve_qn_unknown_name_raises():
    with pytest.raises(api.BiliParseError):
        api.resolve_qn(QUALITIES, "8k ultra")


def test_resolve_qn_empty_qualities_raises():
    with pytest.raises(api.BiliParseError):
        api.resolve_qn([], "auto")


# ── 码流选择 ────────────────────────────────────────────────────
def test_pick_stream_prefers_h264():
    urls = {(64, 12): "hevc", (64, 7): "avc", (64, 13): "av1"}
    assert api.pick_stream_url(urls, 64) == ("avc", 7)


def test_pick_stream_falls_back_to_hevc_then_av1():
    assert api.pick_stream_url({(64, 12): "hevc", (64, 13): "av1"}, 64) == ("hevc", 12)
    assert api.pick_stream_url({(64, 13): "av1"}, 64) == ("av1", 13)


def test_pick_stream_honours_explicit_codec():
    urls = {(80, 7): "avc", (80, 12): "hevc"}
    assert api.pick_stream_url(urls, 80, "hevc") == ("hevc", 12)


def test_pick_stream_supports_plain_qn_keys():
    # durl 模式只有纯 qn 键
    assert api.pick_stream_url({32: "durl"}, 32) == ("durl", 0)
    assert api.pick_stream_url({"32": "durl"}, 32) == ("durl", 0)


def test_pick_stream_returns_empty_when_missing():
    assert api.pick_stream_url({(16, 7): "x"}, 80) == ("", 0)


# ── 文件名 ──────────────────────────────────────────────────────
def test_sanitize_filename_strips_windows_illegal_chars():
    assert BilibiliProvider.sanitize_filename('a/b:c*d?e"f<g>h|i') == "a b c d e f g h i"


def test_sanitize_filename_collapses_whitespace_and_trims():
    assert BilibiliProvider.sanitize_filename("  a \n\n b  ") == "a b"


def test_sanitize_filename_limits_length():
    name = BilibiliProvider.sanitize_filename("标" * 200)
    assert len(name) == 80


def test_unique_path_appends_index(tmp_path):
    first = tmp_path / "视频.mp4"
    first.write_bytes(b"x")
    second = BilibiliProvider._unique_path(first)
    assert second.name == "视频_2.mp4"
    second.write_bytes(b"x")
    assert BilibiliProvider._unique_path(first).name == "视频_3.mp4"


# ── 引擎不可用时的行为 ──────────────────────────────────────────
def test_client_failure_is_cached_and_raised(tmp_path):
    calls = {"n": 0}

    def factory(_storage):
        calls["n"] += 1
        raise ImportError("模拟依赖缺失")

    provider = BilibiliProvider(client_factory=factory)
    storage = type("S", (), {"temp_dir": tmp_path / "tmp"})()

    for _ in range(3):
        with pytest.raises(RuntimeError):
            provider._client_for(storage)

    # 只尝试构造一次，之后走缓存，避免每条消息都重试
    assert calls["n"] == 1


def test_cookie_file_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_BILI_COOKIE_FILE", str(tmp_path / "custom.txt"))
    provider = BilibiliProvider()
    storage = type("S", (), {"temp_dir": tmp_path / "tmp"})()
    assert provider._cookie_file(storage) == tmp_path / "custom.txt"


def test_cookie_file_falls_back_to_convention_path(tmp_path, monkeypatch):
    monkeypatch.delenv("JJ_BILI_COOKIE_FILE", raising=False)
    provider = BilibiliProvider()
    storage = type("S", (), {"temp_dir": tmp_path / "tmp"})()
    assert provider._cookie_file(storage) is None

    cookie = provider._config_dir(storage) / "cookie.txt"
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text("SESSDATA=x", encoding="utf-8")
    assert provider._cookie_file(storage) == cookie


def test_engine_failure_falls_back_to_legacy(tmp_path, monkeypatch):
    """Extension 缺依赖时，下载要退回旧引擎，而不是把异常抛给用户。"""
    from download_system import DownloadResult, DownloadStorage, DownloadTask

    storage = DownloadStorage(tmp_path / "tmp", tmp_path / "out")
    task = DownloadTask(
        kind="bilibili", source="https://www.bilibili.com/video/BV1xx411c7mD"
    )

    def boom(_storage):
        raise ImportError("模拟 pycryptodome 缺失")

    provider = BilibiliProvider(client_factory=boom)
    monkeypatch.setattr(
        BilibiliProvider, "resolve_video_url", staticmethod(lambda url: url)
    )

    sentinel = DownloadResult(path=tmp_path / "x.mp4", size=1, mime="video/mp4")
    seen: dict[str, str] = {}

    def fake_legacy(task, storage, resolved):
        seen["resolved"] = resolved
        return sentinel

    monkeypatch.setattr(provider, "_download_legacy", fake_legacy)

    assert provider._download_sync(task, storage) is sentinel
    assert seen["resolved"].endswith("BV1xx411c7mD")
