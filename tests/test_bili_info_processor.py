import pytest
from pathlib import Path

from download_system.providers.bilibili import BiliInfoProcessor


def test_extract_video_urls_from_text():
    text = """
    先看这个：https://b23.tv/QCpPBGb
    下面是视频：https://www.bilibili.com/video/BV1cEeYezEsD/?share_source=copy_web
    最后一个：https://www.bilibili.com/video/BV1cEeYezEsD
    """
    urls = BiliInfoProcessor.extract_video_urls(text)
    assert urls == [
        "https://b23.tv/QCpPBGb",
        "https://www.bilibili.com/video/BV1cEeYezEsD/?share_source=copy_web",
        "https://www.bilibili.com/video/BV1cEeYezEsD",
    ]


def test_extract_video_url_should_ignore_merged_text():
    text = "https://www.bilibili.com/video/BV1cEeYezEsDThis is a video link"
    assert BiliInfoProcessor.extract_video_urls(text) == []


def test_get_clean_context_from_raw_string():
    event = {
        "raw_message": "【公寓小知识第九期，两位主演居然没同框过。-哔哩哔哩】 https://b23.tv/PNbtvPK",
        "message": "【公寓小知识第九期，两位主演居然没同框过。-哔哩哔哩】 https://b23.tv/PNbtvPK",
    }
    assert "https://b23.tv/PNbtvPK" in BiliInfoProcessor.extract_video_urls(event["raw_message"])
    assert "https://b23.tv/PNbtvPK" in BiliInfoProcessor.extract_video_urls(event["message"])


def test_legacy_runtime_without_helper_method(monkeypatch):
    original = BiliInfoProcessor.get_video_info_from_text
    delattr(BiliInfoProcessor, "get_video_info_from_text")
    try:
        text = "https://b23.tv/QCpPBGb"
        assert BiliInfoProcessor.extract_video_urls(text) == ["https://b23.tv/QCpPBGb"]
    finally:
        BiliInfoProcessor.get_video_info_from_text = original


def test_resolve_short_link(monkeypatch):
    class FakeResponse:
        def __init__(self, url):
            self.url = url
            self.status = 200

    def fake_get_redirect_url(url, timeout=10):
        assert url == "https://b23.tv/QCpPBGb"
        return "https://www.bilibili.com/video/BV1cEeYezEsD/?share_source=copy_web"

    monkeypatch.setattr(BiliInfoProcessor, "_resolve_redirect_url", staticmethod(fake_get_redirect_url))

    result = BiliInfoProcessor.resolve_video_url("https://b23.tv/QCpPBGb")
    assert result == "https://www.bilibili.com/video/BV1cEeYezEsD/?share_source=copy_web"


def test_get_video_info(monkeypatch):
    def fake_fetch_json(url, timeout=10):
        assert "https://api.bilibili.com/x/web-interface/view" in url
        return {
            "code": 0,
            "data": {
                "title": "测试标题",
                "pic": "https://example.com/cover.jpg",
            },
        }

    monkeypatch.setattr(BiliInfoProcessor, "_fetch_json", staticmethod(fake_fetch_json))

    info = BiliInfoProcessor.get_video_info("https://www.bilibili.com/video/BV1cEeYezEsD")
    assert info == {
        "bvid": "BV1cEeYezEsD",
        "title": "测试标题",
        "cover_url": "https://example.com/cover.jpg",
        "url": "https://www.bilibili.com/video/BV1cEeYezEsD",
    }


def test_get_video_download_url(monkeypatch):
    def fake_fetch_json(url, timeout=10):
        if "x/web-interface/view" in url:
            return {
                "code": 0,
                "data": {
                    "title": "测试标题",
                    "pic": "https://example.com/cover.jpg",
                },
            }
        if "x/player/playurl" in url:
            return {
                "code": 0,
                "data": {
                    "durl": [{"url": "https://example.com/download/video.mp4"}],
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(BiliInfoProcessor, "_fetch_json", staticmethod(fake_fetch_json))

    result = BiliInfoProcessor.get_video_download_url("https://www.bilibili.com/video/BV1cEeYezEsD")
    assert result == "https://example.com/download/video.mp4"


def test_get_video_download_url_story_video_uses_cid(monkeypatch):
    def fake_fetch_json(url, timeout=10):
        if "x/web-interface/view" in url:
            return {
                "code": 0,
                "data": {
                    "title": "故事视频",
                    "pic": "https://example.com/cover.jpg",
                    "pages": [{"cid": 37755159101}],
                },
            }
        if "x/player/playurl" in url:
            assert "cid=37755159101" in url, f"missing cid in playurl url: {url}"
            return {
                "code": 0,
                "data": {
                    "durl": [{"url": "https://example.com/download/story_video.mp4"}],
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(BiliInfoProcessor, "_fetch_json", staticmethod(fake_fetch_json))

    result = BiliInfoProcessor.get_video_download_url("https://www.bilibili.com/video/BV1A3oMBTEVg")
    assert result == "https://example.com/download/story_video.mp4"


def test_get_video_download_url_dash_fallback(monkeypatch):
    def fake_fetch_json(url, timeout=10):
        if "x/web-interface/view" in url:
            return {
                "code": 0,
                "data": {"pages": [{"cid": 37755159101}]},
            }
        if "x/player/playurl" in url:
            return {
                "code": 0,
                "data": {
                    "dash": {
                        "video": [
                            {"id": 32, "baseUrl": "https://example.com/download/dash_video.m4s"},
                            {"id": 16, "baseUrl": "https://example.com/download/low.m4s"},
                        ]
                    }
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(BiliInfoProcessor, "_fetch_json", staticmethod(fake_fetch_json))

    result = BiliInfoProcessor.get_video_download_url("https://www.bilibili.com/video/BV1A3oMBTEVg")
    assert result == "https://example.com/download/dash_video.m4s"


def test_get_video_stream_info_exposes_segment_lists(monkeypatch):
    def fake_fetch_json(url, timeout=10):
        if "x/player/playurl" in url:
            return {
                "code": 0,
                "data": {
                    "durl": [
                        {"url": "https://example.com/video/seg1.m4s"},
                        {"url": "https://example.com/video/seg2.m4s"},
                    ],
                    "dash": {
                        "audio": [
                            {"id": 1, "baseUrl": "https://example.com/audio/seg1.m4a"},
                            {"id": 2, "baseUrl": "https://example.com/audio/seg2.m4a"},
                        ]
                    },
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(BiliInfoProcessor, "_fetch_json", staticmethod(fake_fetch_json))

    info = BiliInfoProcessor.get_video_stream_info("https://www.bilibili.com/video/BV1cEeYezEsD")
    assert info["video_url"] == "https://example.com/video/seg1.m4s"
    assert info["video_urls"] == [
        "https://example.com/video/seg1.m4s",
        "https://example.com/video/seg2.m4s",
    ]
    assert info["audio_url"] == "https://example.com/audio/seg2.m4a"
    assert info["audio_urls"] == [
        "https://example.com/audio/seg1.m4a",
        "https://example.com/audio/seg2.m4a",
    ]


def test_download_bytes_retries_on_chunked_error(monkeypatch):
    responses = iter([
        RuntimeError("chunk error"),
        RuntimeError("chunk error"),
        type("FakeResponse", (), {"raise_for_status": lambda self: None, "iter_content": lambda self, chunk_size=8192: [b"video-bytes"]})(),
    ])

    def fake_get(url, timeout=30, stream=True, allow_redirects=True, headers=None):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("download_system.providers.bilibili.requests.get", fake_get)

    result = BiliInfoProcessor._download_bytes("https://example.com/download/video.mp4", retries=2)
    assert result == b"video-bytes"


def test_download_bytes_combines_all_segments(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield self.payload

    def fake_get(url, timeout=30, stream=True, allow_redirects=True, headers=None):
        payload_map = {
            "https://example.com/download/seg1.m4s": b"abc",
            "https://example.com/download/seg2.m4s": b"def",
        }
        return FakeResponse(payload_map[url])

    monkeypatch.setattr("download_system.providers.bilibili.requests.get", fake_get)

    result = BiliInfoProcessor._download_bytes([
        "https://example.com/download/seg1.m4s",
        "https://example.com/download/seg2.m4s",
    ])
    assert result == b"abcdef"


def test_merge_video_audio_to_mp4_reencodes_audio_track(monkeypatch):
    seen = {}

    def fake_which(name):
        return "C:/ffmpeg/bin/ffmpeg.exe" if name == "ffmpeg" else None

    def fake_run(cmd, stdout=None, stderr=None, check=False):
        seen["cmd"] = cmd
        output_path = cmd[-1]
        with open(output_path, "wb") as f:
            f.write(b"merged-mp4")
        return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr("download_system.providers.bilibili.shutil.which", fake_which)
    monkeypatch.setattr("download_system.providers.bilibili.subprocess.run", fake_run)

    result = BiliInfoProcessor._merge_video_audio_to_mp4(b"video-bytes", b"audio-bytes")

    assert result == b"merged-mp4"
    assert "libx264" in seen["cmd"]
    assert "aac" in seen["cmd"]
    assert "-c:v" in seen["cmd"]
    assert "-shortest" not in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-c:v") + 1] == "libx264"


def test_download_video_to_file(monkeypatch, tmp_path):
    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self._body = b"video-bytes"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield self._body

    def fake_get(url, timeout=30, stream=True, allow_redirects=True, headers=None):
        assert url == "https://example.com/download/video.mp4"
        assert headers is not None
        return FakeResponse()

    monkeypatch.setattr("download_system.providers.bilibili.requests.get", fake_get)

    output = BiliInfoProcessor.download_video_to_file(
        "https://example.com/download/video.mp4",
        output_dir=str(tmp_path),
    )

    assert Path(output).exists()
    assert Path(output).read_bytes() == b"video-bytes"
    assert output.endswith(".mp4")
