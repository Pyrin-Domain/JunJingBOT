# -*- coding: utf-8 -*-
"""视频指纹单元测试：用 ffmpeg 现场生成小视频，不依赖网络。"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

import media.video_hash as video_hash_mod
from media.video_hash import (
    clean_video_signatures,
    hash_remote_videos_report,
    video_signature,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


@pytest.fixture()
def sample_video(tmp_path):
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25",
            "-t", "0.6", "-pix_fmt", "yuv420p", "-c:v", "mpeg4",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path


def test_video_signature_has_cover_frame10_and_byte_hash(sample_video):
    signature, report = asyncio.run(
        video_signature(str(sample_video), byte_max_bytes=8 * 1024 * 1024)
    )
    assert signature is not None
    assert len(signature["sig"]) == 64
    assert len(signature["cover"]) == 32
    assert len(signature["frame10"]) == 32
    assert signature["cover"] != signature["frame10"]
    assert len(signature["byte_hash"]) == 64
    assert signature["bytes"] > 0
    assert report["frame_cnt"] == 2


def test_video_byte_hash_respects_cap(sample_video):
    signature, report = asyncio.run(
        video_signature(str(sample_video), byte_max_bytes=1)
    )
    assert signature is not None
    assert signature["sig"]
    assert signature["byte_hash"] == ""
    assert report.get("byte_skip") == "too_large"


def test_video_hash_report_batch(sample_video):
    signatures, report = asyncio.run(
        hash_remote_videos_report([str(sample_video)], max_count=5)
    )
    assert len(signatures) == 1
    assert report["source_cnt"] == 1
    assert report["hashed"] == 1
    assert report["items"][0]["ok"] is True


def test_video_signature_cancels_sibling_when_one_branch_fails(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"not-a-real-video")
    sibling_cancelled = asyncio.Event()

    async def fail_frames(_source, _timeout):
        raise RuntimeError("frame pipeline exploded")

    async def slow_bytes(_source, _timeout, _max_bytes):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return "", 0, {}

    monkeypatch.setattr(video_hash_mod, "_extract_frames", fail_frames)
    monkeypatch.setattr(video_hash_mod, "_hash_bytes", slow_bytes)

    signature, report = asyncio.run(video_signature(str(source)))

    assert signature is None
    assert sibling_cancelled.is_set()
    assert "frame pipeline exploded" in report["error"]


def test_clean_video_signatures_drops_invalid():
    good = "a" * 64
    values = [
        {"sig": good, "byte_hash": "b" * 64, "bytes": 10},
        {"sig": good, "byte_hash": "b" * 64, "bytes": 10},  # duplicate
        {"sig": "short", "byte_hash": "c" * 64, "bytes": 10},
        "not-a-dict",
        {"sig": "", "byte_hash": ""},
    ]
    cleaned = clean_video_signatures(values)
    assert cleaned == [
        {"sig": good, "byte_hash": "b" * 64, "bytes": 10},
        # sig 坏了但字节哈希还在 → 保留，作为精确兜底
        {"sig": "", "byte_hash": "c" * 64, "bytes": 10},
    ]
