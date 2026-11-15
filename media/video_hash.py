# -*- coding: utf-8 -*-
"""视频指纹：封面帧 + 第 10 帧 + 可选的全量字节哈希。

为什么不是「把视频当图片」：
QQ 的 video 段只有 ``file`` / ``url``，没有 ``type=image``。SnowLuma 的
``get_forward_msg`` 返回的视频段又只有 ``{"file": "...mp4", "url": "..."}``，
没有现成封面图；所以「封面帧」只能从视频本身解出来。

QQ 的视频地址是**普通 MP4 + HTTP Range**（不是 HLS/DASH）：

* 实测 18/18 个视频都返回 ``206 Partial Content`` 且 ``Accept-Ranges: bytes``；
* ffmpeg 可以直接把 URL 当输入，按需发 ``Range`` 请求，不需要先下载整个文件；
* 取第 0 帧 + 第 9 帧时，60MB 的样本实际只传了约 1.3MB（见项目日志/测试）。

所以这里默认只解两帧，**不下载整个视频**。全量 SHA-256 仅在文件不超过
``byte_max_bytes`` 时顺带算一下：它适合精确确认，但视频被转码后字节会变，
不能当唯一判据，也不该让一条 60MB 的视频卡住去重闸门。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

try:  # 图片双哈希是现成的 8x8 aHash + 9x8 dHash
    from .image_hash import IMAGE_HASH_HEX_LEN, image_hash
except Exception:  # pragma: no cover - Pillow 缺失时整个模块不可用
    IMAGE_HASH_HEX_LEN = 32
    image_hash = None  # type: ignore[assignment]

try:
    import aiohttp
except Exception:  # pragma: no cover - 环境没装 aiohttp
    aiohttp = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_COUNT = 5
DEFAULT_CONCURRENCY = 2
DEFAULT_BYTE_MAX_BYTES = 8 * 1024 * 1024

VIDEO_SIG_HEX_LEN = 64  # cover(32) + frame10(32)
VIDEO_BYTE_HASH_HEX_LEN = 64  # sha256


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _is_http(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def _short(source: str) -> str:
    text = str(source or "")
    return text[:96] + "…" if len(text) > 96 else text


def _valid_hex(value: str, length: int) -> bool:
    if len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


async def _extract_frames(source: str, timeout: float) -> tuple[list[str], dict]:
    """用 ffmpeg 从 URL/本地文件解出第 0 帧和第 9 帧，返回图片双哈希列表。

    关键点：``-fps_mode passthrough``。不加它时 image2 muxer 会按默认帧率
    复制时间戳，实测会把第 0 帧写成两次（两帧哈希完全相同），第 10 帧就白取了。
    """
    tool = ffmpeg_path()
    info: dict = {}
    if not tool:
        return [], {"error": "ffmpeg_missing"}
    if image_hash is None:
        return [], {"error": "image_hash_unavailable"}

    with tempfile.TemporaryDirectory(prefix="video_hash_") as tmp:
        pattern = str(Path(tmp) / "frame_%02d.png")
        cmd = [
            tool,
            "-v", "error",
            "-nostdin",
            "-i", source,
            "-vf", r"select=eq(n\,0)+eq(n\,9)",
            "-fps_mode", "passthrough",
            "-frames:v", "2",
            "-f", "image2",
            pattern,
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return [], {"error": "timeout"}
        except Exception as exc:  # noqa: BLE001 - 解不出来就安静降级
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return [], {"error": f"{type(exc).__name__}: {exc}"}
        if proc.returncode != 0:
            reason = (stderr or b"").decode("utf-8", "replace")[-300:]
            return [], {"error": "ffmpeg_failed", "stderr": reason}
        files = sorted(Path(tmp).glob("frame_*.png"))
        hashes: list[str] = []
        for path in files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            value = image_hash(data)
            if value:
                hashes.append(value)
        info["frame_cnt"] = len(hashes)
        return hashes, info


def _signature_from_frames(hashes: list[str]) -> tuple[str, str, str]:
    """返回 (cover, frame10, 合并 sig)。只有两帧都在时才给 sig。"""
    cover = hashes[0] if hashes else ""
    frame10 = hashes[1] if len(hashes) > 1 else ""
    sig = cover + frame10 if cover and frame10 else ""
    return cover, frame10, sig


async def _hash_bytes(source: str, timeout: float, max_bytes: int) -> tuple[str, int, dict]:
    """流式算 SHA-256；超过 max_bytes 就放弃，不落盘。"""
    info: dict = {}
    if max_bytes <= 0:
        return "", 0, {"byte_skip": "disabled"}

    if not _is_http(source):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            return "", 0, {"byte_skip": f"{type(exc).__name__}: {exc}"}
        if size > max_bytes:
            return "", size, {"byte_skip": "too_large", "bytes": size}
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        return "", total, {"byte_skip": "too_large", "bytes": total}
                    digest.update(chunk)
        except OSError as exc:
            return "", total, {"byte_skip": f"{type(exc).__name__}: {exc}"}
        return digest.hexdigest(), total, {"bytes": total}

    if aiohttp is None:
        return "", 0, {"byte_skip": "aiohttp_missing"}

    digest = hashlib.sha256()
    total = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                source, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                info["status"] = int(response.status)
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        size = int(length)
                    except ValueError:
                        size = 0
                    info["bytes"] = size
                    if size > max_bytes:
                        return "", size, {"byte_skip": "too_large", "bytes": size}
                if response.status != 200:
                    return "", 0, {"byte_skip": f"http_{response.status}"}
                async for chunk in response.content.iter_chunked(128 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        return "", total, {"byte_skip": "too_large", "bytes": total}
                    digest.update(chunk)
    except asyncio.TimeoutError:
        return "", total, {"byte_skip": "timeout", "bytes": total}
    except Exception as exc:  # noqa: BLE001
        return "", total, {"byte_skip": f"{type(exc).__name__}: {exc}", "bytes": total}
    info["bytes"] = total
    return digest.hexdigest(), total, info


async def video_signature(
    source: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    byte_max_bytes: int = DEFAULT_BYTE_MAX_BYTES,
) -> tuple[dict | None, dict]:
    """给一个视频算 (指纹, 诊断报告)。

    :return: 指纹 dict 或 None。dict 字段：
        ``sig`` = cover + frame10（64 hex，缺一帧则为空）
        ``cover`` / ``frame10`` = 各 32 hex
        ``byte_hash`` = 64 hex（可选）
        ``bytes`` = 文件大小（拿得到时）
    """
    source = str(source or "").strip()
    report: dict = {"source": _short(source)}
    if not source:
        report["error"] = "no_source"
        return None, report
    if _is_http(source):
        pass
    elif not Path(source).exists():
        report["error"] = "not_found"
        return None, report

    # Keep both branches owned by one structured scope. If one unexpectedly fails
    # or the caller is cancelled, TaskGroup cancels the sibling as well instead of
    # leaving an orphan frame-extraction / HTTP request running in the background.
    parallel_error = ""
    try:
        async with asyncio.TaskGroup() as group:
            frames_task = group.create_task(_extract_frames(source, timeout))
            bytes_task = group.create_task(
                _hash_bytes(source, timeout, byte_max_bytes)
            )
    except* Exception as exc_group:
        parallel_error = "; ".join(
            str(exc) for exc in exc_group.exceptions
        )

    if parallel_error:
        report["error"] = f"parallel_failure: {parallel_error}"
        return None, report

    hashes, frame_info = frames_task.result()
    byte_hash, size, byte_info = bytes_task.result()

    cover, frame10, sig = _signature_from_frames(hashes)
    report.update(frame_info)
    report.update(byte_info)
    report.update(
        {
            "cover": cover,
            "frame10": frame10,
            "sig": sig,
            "byte_hash": byte_hash,
            "bytes": int(size or byte_info.get("bytes") or 0),
        }
    )
    if not sig and not byte_hash:
        report.setdefault("error", "no_signature")
        return None, report
    return {
        "sig": sig,
        "cover": cover,
        "frame10": frame10,
        "byte_hash": byte_hash,
        "bytes": int(size or byte_info.get("bytes") or 0),
    }, report


def _pick(sources, max_count: int) -> list[str]:
    result: list[str] = []
    for source in sources or []:
        text = str(source or "").strip()
        if text and text not in result:
            result.append(text)
        if max_count > 0 and len(result) >= max_count:
            break
    return result


async def hash_remote_videos_report(
    sources: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_count: int = DEFAULT_MAX_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
    byte_max_bytes: int = DEFAULT_BYTE_MAX_BYTES,
) -> tuple[list[dict], dict]:
    """批量算视频指纹；失败的视频跳过，报告里留原因。"""
    picked = _pick(sources, max_count)
    report: dict = {
        "sources": [_short(s) for s in picked],
        "source_cnt": len(picked),
        "items": [],
        "hashed": 0,
    }
    if not picked:
        report["error"] = "no_source"
        return [], report

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    results: dict[str, dict | None] = {}
    items: dict[str, dict] = {}

    async def worker(source: str) -> None:
        async with semaphore:
            signature, info = await video_signature(
                source, timeout=timeout, byte_max_bytes=byte_max_bytes
            )
        results[source] = signature
        items[source] = {**info, "ok": signature is not None}
        if signature is None:
            items[source].setdefault("reason", info.get("error") or "no_signature")

    await asyncio.gather(*(worker(source) for source in picked), return_exceptions=True)
    signatures = [results[source] for source in picked if results.get(source)]
    report["items"] = [
        {"source": _short(source), **items.get(source, {"ok": False, "reason": "not_tried"})}
        for source in picked
    ]
    report["hashed"] = len(signatures)
    if not signatures and "error" not in report:
        first = next((i for i in report["items"] if not i.get("ok")), {})
        report["error"] = first.get("reason") or "all_failed"
    return signatures, report


async def hash_remote_videos(
    sources: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_count: int = DEFAULT_MAX_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
    byte_max_bytes: int = DEFAULT_BYTE_MAX_BYTES,
) -> list[dict]:
    signatures, _report = await hash_remote_videos_report(
        sources,
        timeout=timeout,
        max_count=max_count,
        concurrency=concurrency,
        byte_max_bytes=byte_max_bytes,
    )
    return signatures


def clean_video_signatures(values) -> list[dict]:
    """校验/去重视频指纹（库和日志都只认 64 hex 的 sig / byte_hash）。"""
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in values or []:
        if not isinstance(item, dict):
            continue
        sig = str(item.get("sig") or "").strip().lower()
        byte_hash = str(item.get("byte_hash") or "").strip().lower()
        if sig and not _valid_hex(sig, VIDEO_SIG_HEX_LEN):
            sig = ""
        if byte_hash and not _valid_hex(byte_hash, VIDEO_BYTE_HASH_HEX_LEN):
            byte_hash = ""
        if not sig and not byte_hash:
            continue
        key = (sig, byte_hash)
        if key in seen:
            continue
        seen.add(key)
        try:
            size = int(item.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append(
            {
                "sig": sig,
                "byte_hash": byte_hash,
                "bytes": max(0, size),
            }
        )
    return result


__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_COUNT",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_BYTE_MAX_BYTES",
    "VIDEO_SIG_HEX_LEN",
    "VIDEO_BYTE_HASH_HEX_LEN",
    "ffmpeg_path",
    "video_signature",
    "hash_remote_videos_report",
    "hash_remote_videos",
    "clean_video_signatures",
]
