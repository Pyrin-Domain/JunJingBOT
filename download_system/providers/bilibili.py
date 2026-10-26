"""B 站视频解析与下载。

搬运自旧 ``Minitor/bili_info_processor.py``，行为保持：

- 支持普通视频页链接、``b23.tv`` 短链，识别 BV / av 号；
- ``durl`` 优先（每个分段的 URL 依次拼接）；没有 ``durl`` 时取 dash 中
  ``id`` 最大的视频流（最高画质）；
- 音频取 dash 中 ``id`` 最大的音轨；
- 视频 / 音频先落盘，再用 ffmpeg 合并；合并失败回退为无音频视频；
- ffprobe 只用于确认输出文件是否真的有音轨；
- 所有临时文件在 ``finally`` 里清理，绝不把整段视频读进内存。

``download()`` 由 ``DownloadPool`` 调用，可能被重试，因此每次开始都会先
清空本任务的上一次残留文件。
"""

from __future__ import annotations

import asyncio
import gc
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from ..mux import MediaToolError, has_audio_stream, merge_video_audio
from ..storage import DownloadStorage
from ..task import DownloadResult, DownloadTask
from .base import BaseProvider

USER_AGENT = "Mozilla/5.0"
FETCH_HEADERS = {"User-Agent": USER_AGENT}
DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com/",
}
DOWNLOAD_CHUNK_SIZE = 8192
DEFAULT_TIMEOUT = 10
STREAM_TIMEOUT = 30


class BilibiliProvider(BaseProvider):
    kind = "bilibili"

    BILI_VIDEO_RE = re.compile(
        r"https?://(?:[A-Za-z0-9.-]+\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)"
        r"(?:[/?#][^\s<>\"']*)?(?=$|[\s<>\"'\)])"
        r"|https?://b23\.tv/[A-Za-z0-9]+(?:[/?#][^\s<>\"']*)?(?=$|[\s<>\"'\)])",
        re.IGNORECASE,
    )
    BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
    AVID_RE = re.compile(r"(?:^|[/?&=])av(\d+)", re.IGNORECASE)

    # ── 链接提取 / 规范化 ─────────────────────────────────────────
    @staticmethod
    def extract_video_urls(text: str) -> list[str]:
        """从文本中提取所有 B 站视频链接。"""
        if not text:
            return []
        matches = BilibiliProvider.BILI_VIDEO_RE.findall(text)
        return [url for url in matches if url]

    @staticmethod
    def _resolve_redirect_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """获取短链重定向后的真实链接。"""
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        return response.url

    @staticmethod
    def resolve_video_url(url: str) -> str:
        """把短链 / 带锚点链接规范化成可用的视频页面 URL。"""
        if not url:
            return ""
        cleaned = url.strip().split("#", 1)[0].strip()
        if not cleaned:
            return ""
        if "b23.tv" in cleaned.lower():
            return BilibiliProvider._resolve_redirect_url(cleaned)
        return cleaned

    # ── B 站 API ─────────────────────────────────────────────────
    @staticmethod
    def _fetch_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        response = requests.get(url, timeout=timeout, headers=FETCH_HEADERS)
        response.raise_for_status()
        return response.json()

    @classmethod
    def _identify(cls, url: str) -> tuple[str, str]:
        """从规范化后的链接里取 ``(类型, 值)``：``bvid`` 或 ``aid``。"""
        bvid_match = cls.BVID_RE.search(url)
        if bvid_match:
            return "bvid", bvid_match.group(0)
        avid_match = cls.AVID_RE.search(url)
        if avid_match:
            return "aid", avid_match.group(1)
        raise ValueError(f"无法从链接中提取到 BVID / av 号: {url}")

    @staticmethod
    def _playurl_query(kind: str, value: str) -> str:
        # playurl 接口对 av 号使用 ``avid`` 参数，对 BV 号使用 ``bvid``。
        return f"avid={value}" if kind == "aid" else f"bvid={value}"

    @staticmethod
    def get_video_info(url: str) -> dict[str, Any]:
        """根据 B 站视频链接获取标题和封面 url。"""
        normalized_url = BilibiliProvider.resolve_video_url(url)
        key, value = BilibiliProvider._identify(normalized_url)
        api_url = f"https://api.bilibili.com/x/web-interface/view?{key}={value}"
        data = BilibiliProvider._fetch_json(api_url)

        if data.get("code") != 0:
            raise ValueError(f"B 站接口返回错误: {data}")

        video_data = data.get("data") or {}
        # 旧接口只返回这四个字段；``aid`` 属于内部扩展，不放进返回值，
        # 避免老调用方做整字典比较时被打断。
        return {
            "bvid": video_data.get("bvid") or (value if key == "bvid" else ""),
            "title": video_data.get("title", ""),
            "cover_url": video_data.get("pic", ""),
            "url": url,
        }

    # ── 流选择 ───────────────────────────────────────────────────
    @staticmethod
    def _pick_download_stream(data: dict[str, Any]) -> str | None:
        """从 playurl 返回值中抽取一个可直接下载的媒体 URL。"""
        if not isinstance(data, dict):
            return None

        durl = (data.get("data") or {}).get("durl") or []
        if durl:
            urls = [str(item.get("url") or "") for item in durl if (item.get("url") or "").strip()]
            if urls:
                return urls[0]

        dash = (data.get("data") or {}).get("dash") or {}
        if isinstance(dash, dict):
            video_list = dash.get("video") or []
            if video_list:
                best_video = max(video_list, key=lambda item: int(item.get("id") or 0))
                base_url = best_video.get("baseUrl") or best_video.get("url")
                if base_url:
                    return str(base_url)
        return None

    @staticmethod
    def _stream_url(item: dict[str, Any]) -> str:
        return str(item.get("baseUrl") or item.get("url") or "")

    @classmethod
    def _extract_stream_urls(cls, data: dict[str, Any]) -> dict[str, list[str]]:
        """提取视频 / 音频 URL 列表。

        旧版解析的优先级：``durl`` 分段 > dash。dash 只取 id 最大的视频流
        （最高画质），音频同样取 id 最大的音轨。
        """
        info: dict[str, list[str]] = {"video_urls": [], "audio_urls": []}
        if not isinstance(data, dict):
            return info

        payload = data.get("data") or {}
        durl = payload.get("durl") or []
        if durl:
            info["video_urls"] = [
                str(item.get("url") or "")
                for item in durl
                if (item.get("url") or "").strip()
            ]

        # 旧行为：durl 与 dash 可以同时出现；dash 的分段会覆盖 video_urls，
        # 音频则取完整列表（并在最后补一次最高码率音轨）。
        dash = payload.get("dash") or {}
        if isinstance(dash, dict):
            video_list = dash.get("video") or []
            if video_list:
                info["video_urls"] = [
                    cls._stream_url(item)
                    for item in video_list
                    if cls._stream_url(item).strip()
                ]
            audio_list = dash.get("audio") or []
            if audio_list:
                info["audio_urls"] = [
                    cls._stream_url(item)
                    for item in audio_list
                    if cls._stream_url(item).strip()
                ]
                best_audio = max(audio_list, key=lambda item: int(item.get("id") or 0))
                best_audio_url = cls._stream_url(best_audio)
                if best_audio_url:
                    info["audio_urls"].append(best_audio_url)
                    info["audio_urls"] = list(
                        dict.fromkeys(url for url in info["audio_urls"] if url)
                    )

        if not info["video_urls"]:
            video_value = cls._pick_download_stream(data)
            if video_value:
                info["video_urls"] = [str(video_value)]

        if not info["audio_urls"]:
            audio_items = dash.get("audio") or []
            if audio_items:
                best_audio = max(audio_items, key=lambda item: int(item.get("id") or 0))
                audio_url = cls._stream_url(best_audio)
                if audio_url:
                    info["audio_urls"] = [audio_url]

        info["video_urls"] = [url for url in info["video_urls"] if url]
        info["audio_urls"] = [url for url in info["audio_urls"] if url]
        return info

    @staticmethod
    def _best_audio_url(data: dict[str, Any]) -> str:
        dash = (data.get("data") or {}).get("dash") or {}
        audio_items = [
            item
            for item in (dash.get("audio") or [])
            if (item.get("baseUrl") or item.get("url") or "").strip()
        ]
        if not audio_items:
            return ""
        best_audio = max(audio_items, key=lambda item: int(item.get("id") or 0))
        return str(best_audio.get("baseUrl") or best_audio.get("url") or "")

    @classmethod
    def _build_stream_info(
        cls, stream_map: dict[str, list[str]], playurl_data: dict[str, Any]
    ) -> dict[str, Any]:
        video_urls = stream_map.get("video_urls") or []
        audio_urls = stream_map.get("audio_urls") or []
        info: dict[str, Any] = {
            "video_url": video_urls[0],
            "video_urls": video_urls,
            "audio_url": None,
            "audio_urls": audio_urls,
        }
        if audio_urls:
            info["audio_url"] = cls._best_audio_url(playurl_data) or audio_urls[0]
        return info

    @classmethod
    def get_video_stream_info(cls, url: str) -> dict[str, Any]:
        """返回视频 / 音频流地址；结构与旧版一致。"""
        normalized_url = cls.resolve_video_url(url)
        key, value = cls._identify(normalized_url)

        # 先按旧版做法直接请求 playurl（不带 cid，B 站会自动取 p1）。
        playurl_url = (
            f"https://api.bilibili.com/x/player/playurl?"
            f"{cls._playurl_query(key, value)}&qn=80&fnval=16&fourk=1"
        )
        try:
            playurl_data = cls._fetch_json(playurl_url)
            if playurl_data.get("code") == 0:
                stream_map = cls._extract_stream_urls(playurl_data)
                if stream_map.get("video_urls"):
                    return cls._build_stream_info(stream_map, playurl_data)
        except Exception:
            pass

        # 回退：先取 view 里第一页的 cid，再请求 fnval=4048。
        view_data = cls._fetch_json(
            f"https://api.bilibili.com/x/web-interface/view?{key}={value}"
        )
        if view_data.get("code") != 0:
            raise ValueError(f"B 站视频信息接口返回错误: {view_data}")
        pages = (view_data.get("data") or {}).get("pages") or []
        if not pages:
            raise ValueError(f"未找到视频分页信息: {url}")
        cid = pages[0].get("cid")
        if not cid:
            raise ValueError(f"未找到视频 cid: {url}")

        cid_api_url = (
            f"https://api.bilibili.com/x/player/playurl?"
            f"{cls._playurl_query(key, value)}&cid={cid}&qn=80&fnval=4048&fourk=1"
        )
        playurl_data = cls._fetch_json(cid_api_url)
        if playurl_data.get("code") != 0:
            raise ValueError(f"B 站播放地址接口返回错误: {playurl_data}")

        stream_map = cls._extract_stream_urls(playurl_data)
        if not stream_map.get("video_urls"):
            raise ValueError(f"未找到可下载的 B 站视频流: {url}")
        return cls._build_stream_info(stream_map, playurl_data)

    @classmethod
    def get_video_download_url(cls, url: str) -> str:
        """返回一个可直接下载的视频地址。支持短链 / BV / av 页面 URL。"""
        return cls.get_video_stream_info(url)["video_url"]

    @classmethod
    def get_video_info_from_text(cls, text: str) -> list[dict[str, Any]]:
        """从文本中提取所有 B 站链接并返回标题 / 封面信息。"""
        if not text:
            return []
        results: list[dict[str, Any]] = []
        for url in cls.extract_video_urls(text):
            try:
                results.append(cls.get_video_info(url))
            except Exception:
                continue
        return results

    # ── 下载 ─────────────────────────────────────────────────────
    @staticmethod
    def _guess_suffix(url: str) -> str:
        lower = str(url or "").lower().split("?", 1)[0]
        if lower.endswith(".flv"):
            return ".flv"
        return ".mp4"

    @staticmethod
    def _safe_name(prefix: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", prefix or "bili")

    @staticmethod
    def _has_audio_stream(file_path: str | Path) -> bool:
        """兼容旧私有方法：ffprobe 只用于判断音轨。"""
        return has_audio_stream(file_path)

    # ── 旧内存接口（保留给旧调用方 / 测试） ─────────────────────
    @staticmethod
    def _download_bytes(url: str | list[str] | None, retries: int = 3) -> bytes:
        """把一组 URL 下载进内存并拼接（旧接口；新下载走流式落盘）。"""
        if not url:
            raise ValueError("下载地址不能为空")

        urls = url if isinstance(url, list) else [url]
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        all_chunks: list[bytes] = []
        last_exc: Exception | None = None

        for item in urls:
            if not item:
                continue
            for attempt in range(1, retries + 2):
                try:
                    response = requests.get(
                        item,
                        timeout=STREAM_TIMEOUT,
                        stream=True,
                        allow_redirects=True,
                        headers=headers,
                    )
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if chunk:
                            chunks.append(chunk)
                    all_chunks.extend(chunks)
                    gc.collect()
                    break
                except Exception as exc:  # pragma: no cover - 网络异常路径
                    last_exc = exc
                    if attempt <= retries:
                        time.sleep(0.5 * attempt)
                        continue
                    raise

        if not all_chunks and last_exc is not None:
            raise last_exc
        return b"".join(all_chunks)

    @staticmethod
    def _merge_video_audio_to_mp4(
        video_bytes: bytes, audio_bytes: bytes | None
    ) -> bytes:
        """在内存中把视频流和音频流合并为 mp4（旧接口）。"""
        if not audio_bytes:
            return video_bytes
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            return video_bytes

        video_fd, video_path = tempfile.mkstemp(suffix=".m4s")
        audio_fd, audio_path = tempfile.mkstemp(suffix=".m4a")
        output_fd, output_path = tempfile.mkstemp(suffix=".mp4")
        os.close(output_fd)
        try:
            with os.fdopen(video_fd, "wb") as handle:
                handle.write(video_bytes)
            with os.fdopen(audio_fd, "wb") as handle:
                handle.write(audio_bytes)

            proc = subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-i",
                    video_path,
                    "-i",
                    audio_path,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "faststart",
                    "-f",
                    "mp4",
                    output_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            try:
                with open(output_path, "rb") as handle:
                    payload = handle.read()
            except OSError:
                payload = b""

            if (proc.returncode != 0 or not payload) and not has_audio_stream(
                output_path
            ):
                print(
                    "[B站下载] ffmpeg 合并失败，"
                    f"rc={proc.returncode}, "
                    f"stderr={proc.stderr.decode('utf-8', 'ignore')[:2000]}"
                )
                return video_bytes

            gc.collect()
            return payload or b""
        finally:
            for path in (video_path, audio_path, output_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            gc.collect()

    @staticmethod
    def _download_to_file(
        urls: str | list[str] | None, destination: Path, retries: int = 3
    ) -> None:
        """把一组 URL 按顺序流式写入同一个文件（分段拼接）。"""
        if not urls:
            raise ValueError("下载地址不能为空")

        items = urls if isinstance(urls, list) else [urls]
        destination.parent.mkdir(parents=True, exist_ok=True)
        last_exc: Exception | None = None
        written = 0

        with open(destination, "wb") as handle:
            for item in items:
                if not item:
                    continue
                for attempt in range(1, retries + 2):
                    start = handle.tell()
                    response = None
                    try:
                        response = requests.get(
                            item,
                            timeout=STREAM_TIMEOUT,
                            stream=True,
                            allow_redirects=True,
                            headers=DOWNLOAD_HEADERS,
                        )
                        response.raise_for_status()
                        for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            if chunk:
                                handle.write(chunk)
                                written += len(chunk)
                        break
                    except Exception as exc:
                        last_exc = exc
                        # 回滚失败尝试写入的半截数据，下一轮从分段起点重写。
                        handle.seek(start)
                        handle.truncate()
                        if attempt <= retries:
                            time.sleep(0.5 * attempt)
                            continue
                        raise
                    finally:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()

        if not written and last_exc is not None:
            raise last_exc

    @staticmethod
    def _move(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
            return
        except OSError:
            # 跨盘（Windows 不同卷）时 os.replace 会失败，退回复制。
            shutil.copyfile(src, dst)
            try:
                src.unlink()
            except OSError:
                pass

    @classmethod
    def _download_streams(
        cls,
        video_urls: list[str],
        audio_urls: list[str],
        temp_dir: Path,
        output_path: Path,
    ) -> bool:
        """下载视频 / 音频并合并到 ``output_path``；返回是否成功带上音轨。"""
        temp_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:8]
        suffix = cls._guess_suffix(video_urls[0] if video_urls else "")
        video_tmp = temp_dir / f"video_{token}{suffix}"
        audio_tmp = temp_dir / f"audio_{token}.m4a"
        merged_tmp = temp_dir / f"merged_{token}.mp4"

        try:
            cls._download_to_file(video_urls, video_tmp)
            audio_ok = False
            if audio_urls:
                cls._download_to_file(audio_urls, audio_tmp)
                audio_ok = audio_tmp.is_file() and audio_tmp.stat().st_size > 0

            if audio_ok:
                try:
                    merge_video_audio(video_tmp, audio_tmp, merged_tmp)
                    if merged_tmp.is_file() and merged_tmp.stat().st_size > 0:
                        cls._move(merged_tmp, output_path)
                        return True
                except (MediaToolError, OSError) as exc:
                    # 与旧版一致：合并“失败”但输出里确实有音轨时仍采用它。
                    if merged_tmp.is_file() and has_audio_stream(merged_tmp):
                        cls._move(merged_tmp, output_path)
                        return True
                    print(f"[B站下载] ffmpeg 合并失败，回退无音频视频: {exc}")

            cls._move(video_tmp, output_path)
            return False
        finally:
            for leftover in (video_tmp, audio_tmp, merged_tmp):
                try:
                    leftover.unlink()
                except OSError:
                    pass

    @classmethod
    def _download_and_merge(
        cls, source: str, temp_dir: Path, output_path: Path
    ) -> bool:
        """解析 ``source`` 的流并下载；解析失败时把 source 当成直链。"""
        try:
            stream_info = cls.get_video_stream_info(source)
            video_urls = list(stream_info.get("video_urls") or [])
            audio_urls = list(stream_info.get("audio_urls") or [])
        except Exception:
            video_urls = []
            audio_urls = []
        if not video_urls:
            video_urls = [source]
        return cls._download_streams(video_urls, audio_urls, temp_dir, output_path)

    @classmethod
    def download_video_to_file(
        cls,
        video_url: str,
        output_dir: str = "data/downloads",
        filename_prefix: str = "bili",
    ) -> str:
        """把 B 站视频下载到本地并合并音频；返回本地文件路径。"""
        if not video_url:
            raise ValueError("video_url 不能为空")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        safe_name = cls._safe_name(filename_prefix or "bili")
        suffix = cls._guess_suffix(video_url)
        local_path = output_path / f"{safe_name}_{int(time.time())}{suffix}"
        cls._download_and_merge(video_url, output_path, local_path)
        return str(local_path)

    # ── DownloadPool 入口 ────────────────────────────────────────
    async def download(
        self, task: DownloadTask, storage: DownloadStorage
    ) -> DownloadResult:
        return await asyncio.to_thread(self._download_sync, task, storage)

    @staticmethod
    def _clear_dir(path: Path) -> None:
        if not path.is_dir():
            return
        for item in path.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except OSError:
                pass

    def _download_sync(
        self, task: DownloadTask, storage: DownloadStorage
    ) -> DownloadResult:
        source = str(task.source or "").strip()
        if not source:
            raise ValueError("B 站链接不能为空")

        task_dir = storage.task_dir(task)
        # 下载池会重试同一个 task：先清掉上一次尝试留下的文件。
        self._clear_dir(task_dir)

        resolved = self.resolve_video_url(source)

        video_info: dict[str, Any] = {}
        try:
            video_info = self.get_video_info(resolved)
        except Exception:
            video_info = {}

        try:
            stream_info = self.get_video_stream_info(resolved)
            video_urls = list(stream_info.get("video_urls") or [])
            audio_urls = list(stream_info.get("audio_urls") or [])
        except Exception:
            video_urls = []
            audio_urls = []
        if not video_urls:
            # 与旧版一致：解析失败时把输入当成直链再试。
            video_urls = [resolved]

        suffix = self._guess_suffix(video_urls[0])
        default_name = f"bili_{int(time.time())}{suffix}"
        name = self.output_name(task, default_name)
        if not Path(name).suffix:
            name += suffix

        output_dir = task.options.get("output_dir")
        if output_dir:
            destination = Path(str(output_dir)).expanduser() / Path(name).name
        else:
            destination = storage.output_file(task, name)

        has_audio = False
        try:
            has_audio = self._download_streams(
                video_urls, audio_urls, task_dir, destination
            )
            size = destination.stat().st_size if destination.is_file() else 0
            mime = "video/x-flv" if suffix == ".flv" else "video/mp4"
            meta = {
                "bvid": video_info.get("bvid", ""),
                "aid": video_info.get("aid"),
                "title": video_info.get("title", ""),
                "cover_url": video_info.get("cover_url", ""),
                "source_url": source,
                "url": resolved,
                "video_url": video_urls[0],
                "video_urls": video_urls,
                "audio_url": audio_urls[0] if audio_urls else "",
                "segments": len(video_urls),
                "has_audio": bool(has_audio),
            }
            return DownloadResult(
                path=destination,
                size=size,
                mime=mime,
                url=resolved,
                meta=meta,
            )
        finally:
            self._clear_dir(task_dir)


# 旧名字：``Minitor.bili_info_processor.BiliInfoProcessor`` 的迁移版本
BiliInfoProcessor = BilibiliProvider

__all__ = ["BilibiliProvider", "BiliInfoProcessor"]
