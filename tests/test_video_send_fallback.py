"""视频发送的失败识别与换通道兜底。

起因：SnowLuma 报 ``send_group_msg failed: group file upload failed:
code=-134``，但同一个文件过几秒重发就成功。根因有两层：

1. 协议端在「群文件上传失败」时**仍然回正常格式的响应**，只是 ``retcode`` 非 0；
   旧代码不校验，于是把失败当成功 —— 重试和回退文案都不会发生。
2. 即便识别到失败，也没有第二条通道可用。

这里锁住两处：``transport.ensure_ok`` 的判定，以及
``MediaService.send_video_file`` 的回退顺序（file → 重试 file → video）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from AbstractionLayer.kernal import GENERIC
from media.service import MediaService
from transport.api import OneBotAPIError, ensure_ok


# ── retcode 判定 ────────────────────────────────────────────────
def test_ensure_ok_accepts_success():
    assert ensure_ok("x", {"status": "ok", "retcode": 0, "data": {"a": 1}})["data"] == {"a": 1}
    # 有的后端只回 retcode，不带 status
    assert ensure_ok("x", {"retcode": 0}) == {"retcode": 0}
    # 非 dict 原样放行（历史接口偶尔返回列表）
    assert ensure_ok("x", ["raw"]) == ["raw"]


def test_ensure_ok_raises_on_group_file_failure():
    payload = {
        "status": "failed",
        "retcode": -134,
        "message": "文件上传失败（-134）",
    }
    with pytest.raises(OneBotAPIError) as info:
        ensure_ok("send_group_msg", payload)

    assert info.value.retcode == -134
    assert info.value.action == "send_group_msg"
    assert "-134" in str(info.value)
    assert "文件上传失败" in str(info.value)


def test_ensure_ok_treats_unknown_status_as_failure():
    with pytest.raises(OneBotAPIError):
        ensure_ok("send_group_msg", {"status": "async", "retcode": 0})


# ── 发送回退链 ──────────────────────────────────────────────────
class _FakeAPI:
    """按预设序列抛错 / 成功的假 API，记录每次用的是什么段类型。"""

    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.segments: list[str] = []

    async def send_group_message(self, group_id, message, **_kwargs):
        self.segments.append(message[0]["type"])
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


class _FakeImages:
    def to_napcat(self, path):
        return str(path)


def _service(api, **caps_overrides) -> MediaService:
    """默认把重试间隔压到 0，测试别真等；其余能力用通用档案。"""
    options = {"media_retry_delay": 0}
    options.update(caps_overrides)
    return MediaService(
        api=api,
        images=_FakeImages(),
        downloads=object(),
        settings=object(),
        ocr=object(),
        capabilities=replace(GENERIC, **options),
    )


EVENT = {"message_type": "group", "group_id": 123}


def test_retries_file_once_then_succeeds(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    api = _FakeAPI([RuntimeError("retcode=-134"), None])
    ok = __import__("asyncio").run(_service(api).send_video_file(EVENT, video))

    assert ok is True
    assert api.segments == ["file", "file"]


def test_falls_back_to_video_message(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    err = RuntimeError("retcode=-134")
    api = _FakeAPI([err, err, None])
    ok = __import__("asyncio").run(_service(api).send_video_file(EVENT, video))

    assert ok is True
    assert api.segments == ["file", "file", "video"]


def test_raises_when_every_channel_fails(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    err = RuntimeError("retcode=-134")
    api = _FakeAPI([err, err, err])

    with pytest.raises(RuntimeError):
        __import__("asyncio").run(_service(api).send_video_file(EVENT, video))
    assert api.segments == ["file", "file", "video"]


def test_skips_video_channel_for_oversized_file(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"xx")

    err = RuntimeError("retcode=-134")
    api = _FakeAPI([err, err])

    with pytest.raises(RuntimeError):
        __import__("asyncio").run(
            _service(api, video_max_bytes=1).send_video_file(EVENT, video)
        )
    # 超过视频消息上限时不该白试一轮
    assert api.segments == ["file", "file"]


# ── 回退链由能力表驱动，不是写死的 ──────────────────────────────
def test_send_order_comes_from_capabilities(tmp_path):
    """换后端就能换顺序：先视频后文件，不需要改代码。"""
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    api = _FakeAPI([None])
    ok = __import__("asyncio").run(
        _service(api, media_send_order=("video", "file")).send_video_file(EVENT, video)
    )

    assert ok is True
    assert api.segments == ["video"]


def test_retry_times_can_be_disabled(tmp_path):
    """能力表说不用重试就不重试（少等一轮）。"""
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    err = RuntimeError("retcode=-134")
    api = _FakeAPI([err, None])

    ok = __import__("asyncio").run(
        _service(api, media_retry_times=0).send_video_file(EVENT, video)
    )

    assert ok is True
    assert api.segments == ["file", "video"]
