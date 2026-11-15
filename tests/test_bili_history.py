"""``BiliHistory`` 按 BV 号去重下载的测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from media.bili_history import BiliHistory, video_key
from media.service import DEFAULT_SEND_TIMEOUT, MediaService


# ── video_key：BV / av 提取 ────────────────────────────────────
def test_video_key_extracts_bvid_from_page_url():
    assert video_key("https://www.bilibili.com/video/BV1cEeYezEsD") == "BV1cEeYezEsD"


def test_video_key_extracts_bvid_from_short_link():
    # b23.tv 已被 resolve_video_url 展开成正式页，BV 在结果里
    assert video_key("https://www.bilibili.com/video/BV1cEeYezEsD/?p=2") == "BV1cEeYezEsD"


def test_video_key_falls_back_to_av_when_no_bvid():
    assert video_key("https://www.bilibili.com/video/av1234567") == "av1234567"


def test_video_key_empty_when_unresolvable():
    assert video_key("") == ""
    assert video_key("https://b23.tv/abc") == ""  # 短链未展开，没有 BV


# ── BiliHistory：命中跳过下载，文件丢失自动清记录 ──────────────────
def test_get_returns_path_when_file_exists(tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    hist = BiliHistory(tmp_path / "h.json")
    hist.remember("BV1xx", video, title="t")

    assert hist.get("BV1xx") == video


def test_get_returns_none_and_forgets_when_file_gone(tmp_path: Path):
    video = tmp_path / "gone.mp4"
    video.write_bytes(b"x")
    hist = BiliHistory(tmp_path / "h.json")
    hist.remember("BV1xx", video)
    video.unlink()

    assert hist.get("BV1xx") is None
    # 记录被同步清理
    assert hist.get("BV1xx") is None
    raw = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))
    assert "BV1xx" not in raw


def test_remember_then_save_writes_json(tmp_path: Path):
    hist = BiliHistory(tmp_path / "h.json")
    video = tmp_path / "a.mp4"
    video.write_bytes(b"yy")
    hist.remember("BV1zz", video, title="宽衣")
    raw = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))
    assert raw["BV1zz"]["path"] == str(video)
    assert raw["BV1zz"]["title"] == "宽衣"
    assert raw["BV1zz"]["size"] == 2


def test_trim_drops_oldest_beyond_keep(tmp_path: Path):
    hist = BiliHistory(tmp_path / "h.json", keep=2)
    for i, name in enumerate(["a", "b", "c"]):
        p = tmp_path / f"{name}.mp4"
        p.write_bytes(b"x")
        hist.remember(f"BV{i}", p)
    raw = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))
    # 保留最后两条
    assert set(raw) == {"BV1", "BV2"}


def test_empty_key_is_noop(tmp_path: Path):
    hist = BiliHistory(tmp_path / "h.json")
    hist.remember("", tmp_path / "x")  # 不该写库
    assert len(hist) == 0


# ── MediaService.send_bili_video：命中直接走历史文件 ──────────────
class _FakeAPI:
    def __init__(self):
        self.calls = []

    async def send_group_message(self, group_id, message, **kwargs):
        self.calls.append({"group_id": group_id, "type": message[0]["type"]})


class _FakeImages:
    def to_napcat(self, path):
        return str(path)


class _FakeDownloads:
    def __init__(self, path: Path):
        self.path = path
        self.calls = 0

    async def download_bilibili(self, url):
        self.calls += 1
        return SimpleNamespace(path=self.path, meta={"title": "T"})


class _Settings:
    def __init__(self, data_dir: Path | None):
        self._data_dir = data_dir

    def get(self, key, default=None):
        if key == "media.bili_history":
            return True
        return default

    @property
    def paths(self):
        return SimpleNamespace(data_dir=self._data_dir)


EVENT = {"message_type": "group", "group_id": 123}


def _service(api, downloads, settings) -> MediaService:
    from AbstractionLayer.kernal import GENERIC
    from dataclasses import replace

    return MediaService(
        api=api,
        images=_FakeImages(),
        downloads=downloads,
        settings=settings,
        ocr=object(),
        capabilities=replace(GENERIC, media_retry_delay=0),
    )


def test_send_bili_video_skips_download_on_history_hit(tmp_path: Path):
    video = tmp_path / "cached.mp4"
    video.write_bytes(b"video")

    api = _FakeAPI()
    downloads = _FakeDownloads(tmp_path / "new.mp4")
    settings = _Settings(tmp_path)

    svc = _service(api, downloads, settings)

    # 模拟上次成功下载后落过库
    svc._bili_history().remember("BV1cEeYezEsD", video)

    ok = asyncio.run(
        svc.send_bili_video(EVENT, "https://www.bilibili.com/video/BV1cEeYezEsD")
    )

    assert ok is True
    # 没有触发下载
    assert downloads.calls == 0
    # 走的是缓存文件
    assert api.calls[0]["type"] == "file"


def test_send_bili_video_downloads_and_remembers_on_miss(tmp_path: Path):
    video = tmp_path / "fresh.mp4"
    video.write_bytes(b"fresh")

    api = _FakeAPI()
    downloads = _FakeDownloads(video)
    settings = _Settings(tmp_path)

    svc = _service(api, downloads, settings)

    ok = asyncio.run(
        svc.send_bili_video(EVENT, "https://www.bilibili.com/video/BV1cEeYezEsD")
    )

    assert ok is True
    assert downloads.calls == 1
    # 落库了
    assert svc._bili_history().get("BV1cEeYezEsD") == video


def test_send_bili_video_silent_on_timeout(tmp_path: Path):
    """超时不抛、不回复、不重试 —— 用户要求「超时就什么都不做」。"""
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x")

    class TimeoutAPI:
        async def send_group_message(self, group_id, message, **kwargs):
            raise TimeoutError("send_group_msg 在 30 秒内没有响应")

    downloads = _FakeDownloads(video)
    svc = _service(TimeoutAPI(), downloads, _Settings(tmp_path))

    ok = asyncio.run(
        svc.send_bili_video(EVENT, "https://www.bilibili.com/video/BV1cEeYezEsD")
    )

    assert ok is False
    # 下载完且已落库：超时不会让下次重下
    assert downloads.calls == 1
    assert svc._bili_history().get("BV1cEeYezEsD") == video


def test_send_bili_video_no_fallback_message(tmp_path: Path):
    """失败时不该再发任何回执文案（包括「下载地址」）。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    class FailingAPI:
        sent = []

        async def send_group_message(self, group_id, message, **kwargs):
            self.sent.append(message)
            raise RuntimeError("retcode=-134 文件上传失败")

    downloads = _FakeDownloads(video)
    api = FailingAPI()
    svc = _service(api, downloads, _Settings(tmp_path))

    ok = asyncio.run(
        svc.send_bili_video(EVENT, "https://www.bilibili.com/video/BV1cEeYezEsD")
    )

    assert ok is False
    # 只发了原始视频段，没有追加「下载地址：xxx」之类的兜底文本
    assert all(isinstance(m, list) for m in api.sent)
    assert all(m[0]["type"] in {"file", "video"} for m in api.sent)


# ── 默认超时 ────────────────────────────────────────────────────
def test_default_send_timeout_is_600():
    assert DEFAULT_SEND_TIMEOUT == 600.0