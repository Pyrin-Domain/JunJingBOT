"""``bilibilidownloadtool`` 的无界面桥接层。

上游（https://github.com/NANblogink/bilibilidownloadtool）是一个 PyQt5
桌面程序；机器人只要「解析 → 下载 → 合并」三段能力。本文件把这三段
包装成普通 Python 对象，**不引入任何 GUI 依赖**，供
:mod:`download_system.providers.bilibili` 在后台线程里调用。

三个必须知道的坑（改代码前先读）
--------------------------------

1. **扁平导入**：上游 ``parsers/*.py`` 用 ``from wbi_sign import ...``
   这种写法，必须把 ``core/``、``core/infra/``、``parsers/`` 放进
   ``sys.path``。这里用 **append** 而不是 insert —— 项目自己的 ``config``
   包必须永远优先，否则会被上游 ``core/config.py`` 顶掉。
2. **``config`` 同名**：上游配置文件叫 ``config.py``。这里用私有模块名
   ``_bdt_upstream_config`` 加载，不占用 ``sys.modules["config"]``。
3. **分片下载不回填路径**：上游 ``download_file()`` 在文件 ≥2MB 且 CDN
   支持 Range 时走分片并发分支，该分支**直接返回临时文件路径**而不移动到
   ``save_path``。所以每次拿到结果都要走 :func:`_adopt` 搬进自己的任务
   目录 —— 既修正落盘位置，也避免 ``<盘符>:\\_bili_cache`` 里堆临时文件。

线程模型
--------
上游的 ``self.session`` 是有状态的（解析期间会改写 headers / cookies），
因此 :class:`BiliClient` 用一把 ``RLock`` 串行化所有上游调用。视频下载本来
就是重操作，串行换来的是可预测性。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

__all__ = [
    "BiliClient",
    "BiliError",
    "BiliParseError",
    "BiliDownloadError",
    "BiliUnsupported",
    "VideoInfo",
    "Quality",
    "DownloadOutcome",
    "pick_stream_url",
    "resolve_qn",
    "normalize_qualities",
]

logger = logging.getLogger(__name__)

VENDOR_ROOT = Path(__file__).resolve().parent
_UPSTREAM_PATHS = (
    VENDOR_ROOT,
    VENDOR_ROOT / "core",
    VENDOR_ROOT / "core" / "infra",
    VENDOR_ROOT / "parsers",
)

#: B 站 codecid → 编码名。QQ 客户端对 H.264 兼容性最好，默认优先 7。
CODEC_IDS: dict[str, int] = {"h264": 7, "avc": 7, "hevc": 12, "h265": 12, "av1": 13, "vp9": 14}
_CODEC_FALLBACK = (7, 12, 13, 14)

#: 人类可读画质名 → qn
_NAME_TO_QN: dict[str, int] = {
    "360p": 16,
    "480p": 32,
    "720p": 64,
    "720p60": 74,
    "1080p": 80,
    "1080p+": 112,
    "1080p60": 116,
    "4k": 120,
    "hdr": 125,
    "dolby": 126,
    "8k": 127,
}

#: 可下载的媒体类型；音频 / 直播 / UP 主页不在这里（走别的入口）
_SUPPORTED_TYPES = {"video", "av", "bangumi", "cheese", "sponsor"}


# ── 异常 ─────────────────────────────────────────────────────────
class BiliError(RuntimeError):
    """上游桥接层的统一异常基类。"""


class BiliParseError(BiliError):
    """解析失败（链接无效、接口报错、需要登录等）。"""


class BiliDownloadError(BiliError):
    """下载或合并失败。"""


class BiliUnsupported(BiliError):
    """链接类型不支持（直播、音频、UP 主页等）。"""


# ── 上游模块加载 ──────────────────────────────────────────────────
_upstream_lock = threading.Lock()
_upstream: ModuleType | None = None


def _ensure_import_path() -> None:
    """把上游子目录**追加**到 sys.path 末尾（幂等）。"""
    for path in _UPSTREAM_PATHS:
        resolved = os.path.normpath(str(path))
        if resolved not in sys.path:
            sys.path.append(resolved)


def _load_upstream(config_module_name: str = "_bdt_upstream_config") -> ModuleType:
    """加载上游 ``parsers/bilibili_parser.py``，返回该模块。

    上游 ``core/config.py`` 以私有名加载，避免污染 ``sys.modules["config"]``
    （那是本项目自己的配置包）。 ``bilibili_parser`` 本身没有重名风险，直接
    正常 import，这样它内部的扁平 import 才能生效。
    """
    global _upstream
    with _upstream_lock:
        if _upstream is not None:
            return _upstream

        _ensure_import_path()

        config_path = VENDOR_ROOT / "core" / "config.py"
        spec = importlib.util.spec_from_file_location(config_module_name, config_path)
        if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失
            raise BiliError(f"无法加载上游配置模块：{config_path}")
        config_module = importlib.util.module_from_spec(spec)
        sys.modules[config_module_name] = config_module
        spec.loader.exec_module(config_module)

        import bilibili_parser  # noqa: PLC0415 - 必须在上面的 sys.path 就绪后再导入

        bilibili_parser._bdt_config_loader_cls = config_module.ConfigLoader  # type: ignore[attr-defined]
        _upstream = bilibili_parser
        logger.info("bilibilidownloadtool 上游解析器已加载：%s", VENDOR_ROOT)
        return _upstream


# ── 纯函数：画质 / 码流选择（可单测） ─────────────────────────────
@dataclass(frozen=True)
class Quality:
    qn: int
    codecid: int
    name: str


def normalize_qualities(raw: Any) -> list[Quality]:
    """把上游 ``qualities`` 归一化成 :class:`Quality` 列表。

    上游在不同分支里给出 2 元组 ``(qn, name)`` 或 3 元组
    ``(qn, codecid, name)``，这里都要吃得下。
    """
    rows: list[Quality] = []
    for item in raw or []:
        if isinstance(item, (tuple, list)):
            if len(item) >= 3:
                qn, codecid, name = item[0], item[1], item[2]
            elif len(item) == 2:
                qn, name = item[0], item[1]
                codecid = 0
            else:
                continue
        elif isinstance(item, dict):
            qn = item.get("qn") or item.get("id") or 0
            codecid = item.get("codecid") or 0
            name = item.get("name") or item.get("desc") or ""
        else:
            continue
        try:
            rows.append(Quality(int(qn), int(codecid or 0), str(name)))
        except (TypeError, ValueError):
            continue
    # 同一个 qn 可能有多个编码，保留最高编码优先的稳定顺序
    return sorted(rows, key=lambda row: (-row.qn, row.codecid))


def resolve_qn(
    qualities: list[Quality],
    requested: str | int | None = None,
    *,
    max_qn: int | None = None,
) -> int:
    """把 ``"auto" / "1080p" / 80`` 解析成实际可用的 qn。

    规则：
    - ``auto`` / ``None``：取不超过 ``max_qn`` 的最高画质；
    - 数字或 ``"1080p"`` 这类名字：优先精确命中，否则退到「不超过它的最高」，
      再不行就取最低可用，保证总能下一个能播的。
    """
    available = sorted({row.qn for row in qualities if row.qn > 0}, reverse=True)
    if not available:
        raise BiliParseError("该视频没有可用的视频流（可能被地区 / 会员限制）")

    if requested in (None, "", "auto", "best", "max"):
        target: int | None = None
    elif isinstance(requested, int):
        target = requested
    else:
        text = str(requested).strip().lower()
        if text.isdigit():
            target = int(text)
        else:
            target = _NAME_TO_QN.get(text)
            if target is None:
                raise BiliParseError(f"不认识的画质：{requested!r}")

    candidates = available
    if max_qn:
        capped = [qn for qn in available if qn <= int(max_qn)]
        if capped:
            candidates = capped
        elif target is None:
            candidates = [min(available)]

    if target is None:
        return candidates[0]

    if target in candidates:
        return target
    lower = [qn for qn in candidates if qn < target]
    return lower[0] if lower else min(candidates)


def pick_stream_url(
    video_urls: dict[Any, str],
    qn: int,
    prefer_codec: str | int | None = "h264",
) -> tuple[str, int]:
    """按 ``(qn, codecid)`` 取流；编码不可用时按 AVC → HEVC → AV1 回退。

    返回 ``(url, codecid)``；完全没有可用流时返回 ``("", 0)``。
    """
    preferred: int | None = None
    if isinstance(prefer_codec, int):
        preferred = prefer_codec
    elif prefer_codec:
        preferred = CODEC_IDS.get(str(prefer_codec).lower())

    order: list[int] = []
    if preferred and preferred > 0:
        order.append(preferred)
    order.extend(cid for cid in _CODEC_FALLBACK if cid not in order)

    for codecid in order:
        url = video_urls.get((int(qn), codecid))
        if url:
            return str(url), codecid

    # 老接口 / durl 模式只有纯 qn 键
    for key in (qn, str(qn)):
        url = video_urls.get(key)
        if url:
            return str(url), 0
    return "", 0


# ── 数据模型 ─────────────────────────────────────────────────────
@dataclass
class VideoInfo:
    """一次解析的结果（已归一化，不依赖上游 dict 的具体形状）。"""

    url: str
    media_type: str
    bvid: str = ""
    cid: str = ""
    title: str = ""
    cover: str = ""
    duration: int = 0
    qualities: list[Quality] = field(default_factory=list)
    video_urls: dict[Any, str] = field(default_factory=dict)
    audio_url: str = ""
    episodes: list[dict[str, Any]] = field(default_factory=list)
    is_vip: bool = False
    has_hevc: bool = False
    kid: str | None = None
    notice: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def best_qn(self) -> int:
        return self.qualities[0].qn if self.qualities else 0

    def quality_name(self, qn: int) -> str:
        for row in self.qualities:
            if row.qn == qn:
                return row.name
        return f"qn{qn}"

    @property
    def episode_count(self) -> int:
        return len(self.episodes)


@dataclass
class DownloadOutcome:
    """一次下载的产物。"""

    path: Path
    size: int
    title: str
    qn: int
    quality: str
    codecid: int
    has_audio: bool
    duration: int = 0
    bvid: str = ""
    cover: str = ""
    url: str = ""
    warnings: list[str] = field(default_factory=list)


# ── 客户端 ───────────────────────────────────────────────────────
class BiliClient:
    """上游能力的常驻封装：一次构造，多次解析 / 下载。

    参数
    ----
    config_dir:
        上游 ``app_config.json`` 的存放目录（默认 ``<临时目录>/junjing_bili``）。
        不要放进仓库目录，那是代码区。
    cookie_file:
        浏览器 Cookie 文件路径，给了就能拿到登录后才能访问的画质（1080P+ / 大会员）。
    hw_accel:
        预留：上游的 GPU 编码开关；当前只用 copy 模式，不涉及转码。
    """

    def __init__(
        self,
        *,
        config_dir: str | Path | None = None,
        cookie_file: str | Path | None = None,
        max_threads: int = 2,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
    ):
        upstream = _load_upstream()
        loader_cls = getattr(upstream, "_bdt_config_loader_cls", None)
        if loader_cls is None:  # pragma: no cover - 版本不匹配
            raise BiliError("上游版本不匹配：找不到 ConfigLoader")

        self._lock = threading.RLock()
        self._config_dir = self._prepare_config_dir(config_dir)
        self._loader = self._build_loader(loader_cls, max_threads)

        cookie = str(cookie_file) if cookie_file else "cookie.txt"
        self._parser = upstream.BilibiliParser(self._loader, cookie_path=cookie)

        # 上游默认找 ``parsers/ffmpeg/bin/ffmpeg.exe``；我们没随包带二进制，
        # 直接指到系统 ffmpeg，省得它走一遍 which() 并打「未找到」警告。
        ffmpeg_path = ffmpeg or shutil.which("ffmpeg")
        if ffmpeg_path:
            self._parser.ffmpeg_local = ffmpeg_path
        ffprobe_path = ffprobe or shutil.which("ffprobe")
        if ffprobe_path:
            self._parser.ffprobe_local = ffprobe_path

        logger.info("B 站下载客户端就绪：config=%s，ffmpeg=%s", self._config_dir, ffmpeg_path or "未找到")

    # ── 构造辅助 ──
    @staticmethod
    def _prepare_config_dir(config_dir: str | Path | None) -> Path:
        if config_dir is None:
            import tempfile

            path = Path(tempfile.gettempdir()) / "junjing_bili"
        else:
            path = Path(config_dir).expanduser()
        # 转成绝对路径：上游会把 cache_dir 之类的相对路径原样存进配置，
        # 进程换个工作目录就会指到别处。
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _build_loader(self, loader_cls, max_threads: int):
        """构造上游 ConfigLoader，并把配置文件挪到 ``config_dir``。

        上游默认写到仓库根目录；这里先构造再改 ``config_file``，然后重读一次，
        保证已有的用户配置（画质表、headers）不丢。
        """
        loader = loader_cls()
        target = self._config_dir / "app_config.json"
        if Path(loader.config_file).resolve() != target.resolve():
            loader.config_file = str(target)
            loader.config = loader._load_config()  # noqa: SLF001 - 上游没有公开 reload
        if not target.exists():
            loader.save_config()

        try:
            wanted = max(1, int(max_threads))
            if int(loader.get_app_setting("max_threads", 0) or 0) != wanted:
                loader.set_app_setting("max_threads", wanted)
            # 上游初始化时用 cache_dir 决定 safe_temp_dir。注意 download_file()
            # 内部的临时目录另算（按目标盘符取 <盘符>:\_bili_cache），那部分
            # 由 _adopt() 搬走，见模块文档第 3 条。
            cache_dir = str(self._config_dir / "cache")
            if str(loader.get_app_setting("cache_dir", "") or "") != cache_dir:
                loader.set_app_setting("cache_dir", cache_dir)
        except Exception as exc:  # pragma: no cover - 配置写入失败不该阻断下载
            logger.warning("写入上游配置失败（忽略）：%s", exc)
        return loader

    # ── 解析 ──
    def parse(self, url: str, *, page: int | None = None, max_episodes: int = 200) -> VideoInfo:
        """解析链接，返回归一化的 :class:`VideoInfo`。"""
        target = str(url or "").strip()
        if not target:
            raise BiliParseError("B 站链接为空")

        with self._lock:
            meta = self._parser.parse_media_url(target)
            media_type = str(meta.get("type") or "").strip()
            media_id = str(meta.get("id") or "").strip()
            if meta.get("error"):
                raise BiliParseError(f"链接解析失败：{meta['error']}")
            if not media_type or not media_id:
                raise BiliParseError(f"无法识别的 B 站链接：{target}")
            if media_type not in _SUPPORTED_TYPES:
                raise BiliUnsupported(
                    f"暂不支持的类型：{media_type}（只支持普通投稿 / 番剧 / 课程）"
                )

            page_no = page or meta.get("page")
            try:
                page_no = int(page_no) if page_no else None
            except (TypeError, ValueError):
                page_no = None

            mode = "page_only" if page_no else "video_only"
            raw = self._parser.parse_media(
                media_type,
                media_id,
                parse_mode=mode,
                episode_page=page_no,
                max_episodes=max(1, int(max_episodes)),
            )

        if not isinstance(raw, dict):
            raise BiliParseError("解析接口返回了意外结果")
        if not raw.get("success"):
            raise BiliParseError(str(raw.get("error") or "解析失败"))

        return self._to_info(target, media_type, raw)

    @staticmethod
    def _to_info(url: str, media_type: str, raw: dict[str, Any]) -> VideoInfo:
        qualities = normalize_qualities(raw.get("qualities"))
        episodes = [
            {
                "page": ep.get("page"),
                "bvid": ep.get("bvid") or raw.get("bvid") or "",
                "cid": ep.get("cid"),
                "title": ep.get("ep_title") or ep.get("title") or "",
                "duration": ep.get("duration") or 0,
                "denied": bool(ep.get("permission_denied")),
                "free_part": bool(ep.get("has_free_part")),
            }
            for ep in (raw.get("episodes") or raw.get("collection") or [])
            if isinstance(ep, dict)
        ]
        return VideoInfo(
            url=url,
            media_type=media_type,
            bvid=str(raw.get("bvid") or ""),
            cid=str(raw.get("cid") or ""),
            title=str(raw.get("title") or raw.get("season_title") or "").strip(),
            cover=str(raw.get("pic") or raw.get("cover") or "").strip(),
            duration=int(raw.get("total_duration") or 0),
            qualities=qualities,
            video_urls=dict(raw.get("video_urls") or {}),
            audio_url=str(raw.get("audio_url") or ""),
            episodes=episodes,
            is_vip=bool(raw.get("is_vip")),
            has_hevc=bool(raw.get("has_hevc")),
            kid=raw.get("kid"),
            notice=str(raw.get("preview_notice") or ""),
            raw=raw,
        )

    # ── 下载 ──
    def download(
        self,
        url: str,
        destination: str | Path,
        *,
        quality: str | int | None = "auto",
        page: int | None = None,
        prefer_codec: str | int | None = "h264",
        max_qn: int | None = None,
        info: VideoInfo | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> DownloadOutcome:
        """下载单个视频（视频轨 + 音频轨，ffmpeg 合流）到 ``destination``。"""
        target = Path(destination)
        info = info or self.parse(url, page=page)

        warnings: list[str] = []
        if info.notice:
            warnings.append(info.notice)
        if not info.video_urls:
            raise BiliParseError("该视频没有可下载的视频流")

        qn = resolve_qn(info.qualities, quality, max_qn=max_qn)
        video_url, codecid = pick_stream_url(info.video_urls, qn, prefer_codec)
        if not video_url:
            raise BiliDownloadError(f"qn={qn} 没有可用的码流")

        if codecid not in (0, 7):
            warnings.append(f"该视频只有编码 {codecid}（非 H.264），部分手机可能无法播放")

        callback = progress or (lambda *_args: None)
        target.parent.mkdir(parents=True, exist_ok=True)

        token = os.urandom(4).hex()
        video_tmp = target.parent / f"_v_{token}.m4s"
        audio_tmp = target.parent / f"_a_{token}.m4s"
        leftovers = [video_tmp, audio_tmp]

        try:
            with self._lock:
                self._download_track(
                    video_url, video_tmp, callback, "video", info.bvid, info.kid
                )
                has_audio = False
                if info.audio_url:
                    try:
                        self._download_track(
                            info.audio_url, audio_tmp, callback, "audio", info.bvid, info.kid
                        )
                        has_audio = audio_tmp.is_file() and audio_tmp.stat().st_size > 0
                    except Exception as exc:  # 音频失败不该毁掉整个视频
                        warnings.append(f"音频轨下载失败，将发送无声视频：{exc}")

                merged = False
                if has_audio:
                    ok, message = self._parser.merge_media(
                        str(video_tmp),
                        str(audio_tmp),
                        str(target),
                        kid=info.kid,
                        video_process_mode="copy",
                    )
                    merged = bool(ok) and target.is_file() and target.stat().st_size > 0
                    if not merged:
                        warnings.append(f"合并音轨失败，回退无声视频：{message}")
                if not merged:
                    _adopt(video_tmp, target)
                    has_audio = False

            if not target.is_file() or target.stat().st_size <= 0:
                raise BiliDownloadError("下载结束但目标文件为空")

            return DownloadOutcome(
                path=target,
                size=target.stat().st_size,
                title=info.title,
                qn=qn,
                quality=info.quality_name(qn),
                codecid=codecid,
                has_audio=has_audio,
                duration=info.duration,
                bvid=info.bvid,
                cover=info.cover,
                url=info.url,
                warnings=warnings,
            )
        finally:
            with self._lock:
                for leftover in leftovers:
                    _silent_remove(leftover)

    def _download_track(
        self,
        stream_url: str,
        target: Path,
        callback: Callable[[int, int], None],
        kind: str,
        bvid: str,
        kid: str | None,
    ) -> Path:
        """调上游 ``download_file`` 并修正它返回的临时路径。"""
        returned = self._parser.download_file(
            stream_url,
            str(target),
            callback,
            kind,
            bvid or None,
            None,
            kid,
        )
        return _adopt(returned, target)

    def close(self) -> None:
        """关闭底层 HTTP 会话（上游没有公开 close，这里尽量释放）。"""
        with self._lock:
            for attr in ("session", "_download_session"):
                session = getattr(self._parser, attr, None)
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover - 关闭失败无需关心
                        pass


# ── 文件搬运 ─────────────────────────────────────────────────────
def _adopt(returned: str | Path, target: Path) -> Path:
    """把上游返回的文件搬到 ``target``。

    上游分片分支会直接返回 ``<盘符>:\\_bili_cache\\temp_xxx.m4s``，不搬到调用方
    给的 ``save_path``；这里统一收口，顺便把临时文件挪出系统盘。
    """
    source = Path(str(returned or ""))
    if not source.is_file():
        raise BiliDownloadError(f"下载产物不存在：{source}")
    if source.resolve() == target.resolve():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        shutil.move(str(source), str(target))
    return target


def _silent_remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
