"""``img_fd`` 的批量图片搬运行为。

重点：``-g`` 指的就是命令所在的群时**也要发**（显式命令不该被静默跳过），
以及空区间 / 发送失败都要在日志里说清楚，而不是「看起来跑完了」。
"""
import asyncio
from types import SimpleNamespace

from bot.services.forward import ForwardService

GROUP = 20001


def _history_msgs():
    """模拟区间内的消息：两张图 + 一条文本 + 一条带视频的消息。"""
    return [
        {"message_id": 11, "message_seq": 11, "message": [{"type": "image", "data": {"file": "a.jpg"}}]},
        {"message_id": 12, "message_seq": 12, "message": [{"type": "text", "data": {"text": "聊天"}}]},
        {"message_id": 13, "message_seq": 13, "message": [{"type": "video", "data": {"file": "v.mp4"}}]},
        {"message_id": 14, "message_seq": 14, "message": [{"type": "image", "data": {"file": "b.jpg"}}]},
    ]


class _FakeMP:
    def __init__(self, msgs=None):
        self.msgs = msgs if msgs is not None else _history_msgs()
        self.scanned = []

    async def ensure_connected(self, *a, **kw):
        return True

    def is_contain_image(self, event):
        return any(seg.get("type") == "image" for seg in event.get("message") or [])

    def is_contain_video(self, event):
        return any(seg.get("type") == "video" for seg in event.get("message") or [])

    async def get_long_history_only_processor(self, group_id, start_id, end_id, processor, params):
        # 简化版：区间内的消息按顺序交给处理器，命中 end_id 就停
        for item in self.msgs:
            await processor(event=item, **params)
            if item["message_seq"] == int(end_id):
                return


class _FakeAPI:
    def __init__(self, *, status="ok", retcode=0, timeout=False):
        self.calls = []
        self.status = status
        self.retcode = retcode
        self.timeout = timeout

    async def send_group_forward_msg(self, group_id, messages, timeout=None):
        self.calls.append(("group_forward", group_id, len(messages)))
        if self.timeout:
            raise TimeoutError("send_group_forward_msg 在 180 秒内没有响应")
        return {"status": self.status, "retcode": self.retcode, "data": {"message_id": 9}}


def _service(**kw):
    api = _FakeAPI(**kw)
    svc = ForwardService(
        mp=_FakeMP(),
        api=api,
        settings=SimpleNamespace(get=lambda key, default=None: default,
                                 reply=lambda key, default="": default),
    )
    return svc, api


def _run(**kw):
    svc, api = _service(**kw)
    asyncio.run(
        svc.image_forward_batch(
            group_id=GROUP, target_id_list=[GROUP], begin_id=10, end_id=14, msg_type="group"
        )
    )
    return api


def test_same_group_target_is_sent():
    """-g 写命令所在群时必须真的发出去（旧逻辑会 continue 掉，群里什么都没有）。"""
    api = _run()
    assert api.calls == [("group_forward", GROUP, 2)]


def test_only_images_are_collected():
    api = _run()
    _, _, count = api.calls[0]
    assert count == 2  # 文本和视频都不进 node


def test_failed_send_is_reported(capsys):
    _run(status="failed", retcode=1200)
    out = capsys.readouterr().out
    assert "发送失败" in out


def test_timeout_counts_as_sent(capsys):
    _run(timeout=True)
    out = capsys.readouterr().out
    assert "按已发出处理" in out


def test_empty_range_skips_send(capsys):
    svc, api = _service()
    svc.mp = _FakeMP(msgs=[{"message_id": 14, "message_seq": 14,
                            "message": [{"type": "text", "data": {"text": "只有文字"}}]}])
    asyncio.run(
        svc.image_forward_batch(
            group_id=GROUP, target_id_list=[GROUP], begin_id=10, end_id=14, msg_type="group"
        )
    )
    assert api.calls == []
    assert "没有可搬运的图片" in capsys.readouterr().out
