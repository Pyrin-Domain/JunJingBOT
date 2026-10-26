"""media / MessageProcessor 的 B 站与点歌编排（不联网）。"""

import asyncio
from types import SimpleNamespace

from media.service import MediaService
from messaging.processor import MessageProcessor


class _FakeMedia:
    def __init__(self):
        self.sent = []
        self.ark_text = None

    async def send_bili_video(self, event, url):
        self.sent.append(url)
        return True

    async def get_bili_ark_from_text(self, text):
        self.ark_text = text
        return {"type": "bili", "jumpUrl": "https://www.bilibili.com/video/BV1cEeYezEsD"}


class _BiliMP(MessageProcessor):
    def __init__(self, media):
        super().__init__(api=None, images=None)
        self.media = media
        self._clean = ""
        self._replied = None
        self._miniapp = None

    def get_clean_context(self, *args, **kwargs):
        return self._clean

    def is_reply(self, event):
        return "1" if self._replied is not None else None

    async def get_replied_message(self, event):
        return self._replied

    def check_is_miniapp(self, event):
        return self._miniapp or {}


def test_try_send_bili_video_resolves_text_and_delegates():
    async def run():
        media = _FakeMedia()
        mp = _BiliMP(media)
        mp._clean = "看这个 https://www.bilibili.com/video/BV1cEeYezEsD 好看"
        ok = await mp.try_send_bili_video({"message_type": "group"})
        return ok, media

    ok, media = asyncio.run(run())
    assert ok is True
    assert media.sent == ["https://www.bilibili.com/video/BV1cEeYezEsD"]


def test_try_send_reply_bili_card_uses_miniapp_link():
    async def run():
        media = _FakeMedia()
        mp = _BiliMP(media)
        mp._clean = ""
        mp._replied = {"message": []}
        mp._miniapp = {"link": "https://www.bilibili.com/video/BV1cEeYezEsD"}
        ok = await mp.try_send_reply_bili_card({"message_type": "group"})
        return ok, media

    ok, media = asyncio.run(run())
    assert ok is True
    assert media.sent == ["https://www.bilibili.com/video/BV1cEeYezEsD"]


def test_get_bili_ark_from_event_uses_clean_text():
    async def run():
        media = _FakeMedia()
        mp = _BiliMP(media)
        mp._clean = "https://www.bilibili.com/video/BV1cEeYezEsD"
        return await mp.get_bili_ark_from_event({"message_type": "group"}), media

    info, media = asyncio.run(run())
    assert info["type"] == "bili"
    assert media.ark_text == "https://www.bilibili.com/video/BV1cEeYezEsD"


class _FakeAPI:
    def __init__(self):
        self.calls = []

    async def send_group_message(self, **kwargs):
        self.calls.append(kwargs)


class _FakeDownloads:
    async def download_music(self, keyword):
        assert keyword == "晴天"
        return SimpleNamespace(meta={"music_url": "https://example.com/song.mp3"})


class _FakeOCR:
    async def recognize(self, url, re_ocr=False):
        return {"text": ""}


def test_send_music_keeps_old_group_record_shape():
    async def run():
        api = _FakeAPI()
        media = MediaService(
            api=api,
            images=None,
            downloads=_FakeDownloads(),
            settings=None,
            ocr=_FakeOCR(),
        )
        ok = await media.send_music({"message_type": "group", "group_id": 123}, "晴天")
        return ok, api

    ok, api = asyncio.run(run())
    assert ok is True
    assert api.calls == [
        {
            "group_id": 123,
            "message": [
                {"type": "record", "data": {"url": "https://example.com/song.mp3"}}
            ],
        }
    ]
