# -*- coding: utf-8 -*-
"""图片过滤门：过滤函数 + 回调，配置驱动的可开关列表。

设计目标只有一句话：**以后加一种过滤，只是往注册表里加一个函数，不是改搬运
主流程**。主流程只问两件事：

* 这张图 / 这条聊天记录里的图，命中哪些 ``FilterHit``？
* 命中后交给哪个回调（默认打标签发到审计群）。

配置形状（``bot_config.image_filter``）::

    {
      "enable": true,
      "audit_group_id": 1032125562,
      "single_tag_mode": "single",       # single | forward_reply
      "record_tag_mode": "reply",        # reply | before | after
      "block_matched": true,
      "scan_all_images": true,
      "max_images": 0,                   # 0 = 不设上限（别再卡张数，卡了等于漏判）
      "filters": [
        {"id": "phoebe_bullshit", "enable": true, "params": {...}},
        {"id": "nsfw", "enable": false, "params": {...}}
      ]
    }

``filters`` 列表就是「当前启用的过滤函数」。每次判定都从配置重新构建列表，
所以热更新只要改这个列表；删掉一项就是停用，加一项就是启用（函数必须在
:func:`default_registry` 里注册过）。未知 id 直接跳过，不影响其它过滤。

本模块只做「下载 + 判定」，不发 QQ 消息。发送 / 打标签在
:mod:`bot.services.forward.image_gate`，这样纯逻辑可以单独测试。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from AbstractionLayer.segments import iter_photos
from core.logging import get_logger
from messaging.forward_filter import message_text

log = get_logger("messaging.image_filter")

try:  # Pillow 缺失时图片类过滤全部安静降级
    from PIL import Image, ImageOps

    IMAGE_AVAILABLE = True
except Exception:  # pragma: no cover - 环境没装 Pillow
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    IMAGE_AVAILABLE = False


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════
@dataclass
class ImageProbe:
    """一张待判定的图片 + 它所在的上下文。

    ``data`` 可能是 ``None``（没 URL / 下载失败）。这种情况下仍然会跑过滤：
    像「刚刚有人发了菲比暴论，紧接着机器人发图」这种上下文过滤不需要图片字节。
    """

    url: str = ""
    data: bytes | None = None
    file_id: str = ""  # image 段的 file 字段（= 图片 md5 + 扩展名），本地缓存查找用
    file_size: int = 0  # 段里带的文件大小（字节；NapCat 有，SnowLuma 没有），下载前预检用
    source: str = ""  # single_image / chat_record
    event: dict = field(default_factory=dict)  # 外层事件（聊天记录 = 卡片那条消息）
    message: dict = field(default_factory=dict)  # 图片所在的那条消息（聊天记录 = 内层）
    group_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    message_id: str = ""  # 图片所在消息的 id（聊天记录 = **内层** id，取不回原消息）
    text: str = ""
    index: int = 0
    context: dict = field(default_factory=dict)
    error: str = ""  # 下载失败原因（成功为空；只给报告 / 回显看，不影响判定）


@dataclass
class FilterHit:
    """一个过滤函数的判定结果。

    ``label`` 是给人看的类型名（打标签就用它）；``filter_id`` 是配置里的 id。
    """

    filter_id: str
    label: str
    probe: ImageProbe
    score: float | None = None
    detail: str = ""
    manual: bool = False  # True = 只审计不拦截（黑白图待人工确认）


FilterFn = Callable[[ImageProbe, dict], FilterHit | None]


@dataclass(frozen=True)
class FilterSpec:
    filter_id: str
    label: str
    match: FilterFn
    default_enabled: bool = True
    description: str = ""


# ══════════════════════════════════════════════════════════
# 配置归一化
# ══════════════════════════════════════════════════════════
#: 单条聊天记录最多判定多少张图；**0 = 不设上限**（默认）。
#:
#: 以前默认 12，大记录（实测有 112 张图的）后面的图直接不判，等于漏判。
#: 现在默认全量：``short_circuit`` 开着时命中即停，成本由它兜底。
_DEFAULT_MAX_IMAGES = 0
_DEFAULT_IMAGE_TIMEOUT = 8.0
_DEFAULT_COMMAND_WINDOW = 90.0
_DEFAULT_TAG_IMAGE_LIMIT = 6
#: 单张图的下载上限（字节）。QQ 群图常见 8~9MB 的原图/GIF，
#: 卡 8MB 会让这些图「下载失败 → 不判定」，也是漏判来源之一。
_DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _number(value: Any, default: float, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _integer(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def normalize_filter_config(raw: Any) -> dict:
    """把 ``image_filter`` 归一成判定时使用的 dict。

    容忍 ``None`` / ``False`` / 不完整配置；不认识的字段原样保留，方便以后扩展。
    """
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        return {
            "enable": False,
            "audit_group_id": "",
            "single_tag_mode": "single",
            "record_tag_mode": "reply",
            "block_matched": True,
            "scan_all_images": True,
            "max_images": _DEFAULT_MAX_IMAGES,
            "image_timeout": _DEFAULT_IMAGE_TIMEOUT,
            "max_image_bytes": _DEFAULT_MAX_IMAGE_BYTES,
            "max_tag_images": _DEFAULT_TAG_IMAGE_LIMIT,
            "filters": None,
        }

    return {
        "enable": _bool(raw.get("enable"), False),
        "audit_group_id": str(raw.get("audit_group_id") or "").strip(),
        "single_tag_mode": _choice(
            raw.get("single_tag_mode"), {"single", "forward_reply"}, "single"
        ),
        "record_tag_mode": _choice(
            raw.get("record_tag_mode"), {"reply", "before", "after"}, "reply"
        ),
        "block_matched": _bool(raw.get("block_matched"), True),
        "scan_all_images": _bool(raw.get("scan_all_images"), True),
        "max_images": _integer(raw.get("max_images"), _DEFAULT_MAX_IMAGES, minimum=0),
        "image_timeout": _number(
            raw.get("image_timeout"), _DEFAULT_IMAGE_TIMEOUT, minimum=0.1
        ),
        "max_image_bytes": _integer(
            raw.get("max_image_bytes"), _DEFAULT_MAX_IMAGE_BYTES, minimum=0
        ),
        "max_tag_images": _integer(
            raw.get("max_tag_images"), _DEFAULT_TAG_IMAGE_LIMIT, minimum=1
        ),
        "filters": raw.get("filters") if isinstance(raw.get("filters"), list) else None,
        # 命中即停：按批（4 张）下载+判定，一批命中就不再碰后面的图，省流量
        "short_circuit": _bool(raw.get("short_circuit"), False),
        # NapCat 代下载兜底（download_file / 本地缓存）：不认识也原样带出去
        "napcat_http": str(raw.get("napcat_http") or "").strip(),
        "napcat_token": str(raw.get("napcat_token") or "").strip(),
    }


# ══════════════════════════════════════════════════════════
# 最近命令追踪（菲比暴论这类「先命令、后出图」）
# ══════════════════════════════════════════════════════════
class RecentCommandTracker:
    """按群记录最近一条消息文本，供上下文过滤使用。

    为什么需要它：机器人出图事件本身没有文字，而触发命令是上一条**别的用户**
    发的消息。事件是分开推过来的，主流程拿不到上一条，只能自己记一小段时间。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, tuple[float, str]] = {}

    def note_event(self, event: dict | None) -> None:
        event = event or {}
        group_id = str(event.get("group_id") or "").strip()
        if not group_id:
            return
        text = message_text(event).strip()
        if not text:
            return
        with self._lock:
            self._last[group_id] = (time.monotonic(), text[:200])

    def recent(self, group_id: str | None) -> tuple[str, float]:
        """返回 ``(最近文本, 距今秒数)``；没有记录时返回 ``("", inf)``。"""
        key = str(group_id or "").strip()
        if not key:
            return "", float("inf")
        with self._lock:
            item = self._last.get(key)
        if not item:
            return "", float("inf")
        stamp, text = item
        return text, max(0.0, time.monotonic() - stamp)

    def clear(self) -> None:
        with self._lock:
            self._last.clear()


# ══════════════════════════════════════════════════════════
# 注册表 / 配置驱动的启用列表
# ══════════════════════════════════════════════════════════
class FilterRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, FilterSpec] = {}

    def register(self, spec: FilterSpec) -> None:
        self._specs[spec.filter_id] = spec

    def get(self, filter_id: str) -> FilterSpec | None:
        return self._specs.get(str(filter_id or "").strip())

    def all(self) -> list[FilterSpec]:
        return list(self._specs.values())

    def active(self, config: dict) -> list[tuple[FilterSpec, dict, dict]]:
        """按配置返回 ``[(spec, params, entry), ...]``。

        ``filters`` 缺失时用注册表的默认开关；给了列表就**只听列表的**：
        列表里没有的过滤函数处于停用状态，列表里多余的未知 id 忽略。
        """
        entries = config.get("filters")
        if not isinstance(entries, list):
            entries = [
                {"id": spec.filter_id, "enable": spec.default_enabled}
                for spec in self._specs.values()
            ]
        active: list[tuple[FilterSpec, dict, dict]] = []
        for entry in entries:
            if isinstance(entry, str):
                entry = {"id": entry}
            if not isinstance(entry, dict):
                continue
            filter_id = str(entry.get("id") or entry.get("filter_id") or "").strip()
            spec = self._specs.get(filter_id)
            if spec is None:
                continue
            if not _bool(entry.get("enable"), spec.default_enabled):
                continue
            params = entry.get("params")
            active.append((spec, params if isinstance(params, dict) else {}, entry))
        return active


# ══════════════════════════════════════════════════════════
# 内置过滤：菲比暴论
# ══════════════════════════════════════════════════════════
def image_layout(data: bytes | None) -> dict | None:
    """算「左图右黑底白字」版式的几个特征；不是图片返回 ``None``。

    菲比暴论样本（1800x600）实测：右半灰度均值 ~14，右半白字占比 ~0.4%，
    左半灰度均值 70~105，宽高比 3.0。这里只提供特征，阈值交给过滤函数，
    以后同类模板换尺寸 / 换颜色也只需要改配置。
    """
    if not IMAGE_AVAILABLE or not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as raw:
            if getattr(raw, "n_frames", 1) > 1:
                raw.seek(0)
            image = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = image.size
            if width < 200 or height < 80:
                return None
            half = width // 2
            left = image.crop((0, 0, half, height)).convert("L")
            right = image.crop((half, 0, width, height)).convert("L")
            left_hist = left.histogram()
            right_hist = right.histogram()
            left_total = max(1, half * height)
            right_total = max(1, (width - half) * height)
            left_mean = sum(i * n for i, n in enumerate(left_hist)) / left_total
            right_mean = sum(i * n for i, n in enumerate(right_hist)) / right_total
            # 白字：右半灰度 > 180 的像素占比
            right_white = sum(right_hist[181:]) / right_total
            left_white = sum(left_hist[181:]) / left_total
            return {
                "width": width,
                "height": height,
                "ratio": width / height if height else 0.0,
                "left_mean": round(left_mean, 1),
                "right_mean": round(right_mean, 1),
                "right_white": round(right_white, 4),
                "left_white": round(left_white, 4),
            }
    except Exception:
        return None


def match_phoebe_bullshit(probe: ImageProbe, params: dict) -> FilterHit | None:
    """菲比暴论：同群最近有人发过触发词，或图片版式命中模板。

    主判据是上下文（触发词 + 发送者 + 群），因为模板以后可能换；版式只做兜底，
    比如 Bot 重启后丢了最近消息、或触发词窗口已经过期。
    """
    sender_ids = {
        str(item).strip()
        for item in (params.get("sender_ids") or ["3792417235"])
        if str(item).strip()
    }
    group_ids = {
        str(item).strip()
        for item in (params.get("group_ids") or ["740726382"])
        if str(item).strip()
    }
    if sender_ids and str(probe.sender_id or "").strip() not in sender_ids:
        return None
    if group_ids and str(probe.group_id or "").strip() not in group_ids:
        return None

    command = str(params.get("command") or "菲比暴论")
    window = _number(params.get("command_window"), _DEFAULT_COMMAND_WINDOW, minimum=0)
    recent_text = str(probe.context.get("recent_text") or "")
    recent_age = _number(probe.context.get("recent_age"), float("inf"), minimum=0)
    reason = ""
    if command and command in recent_text and recent_age <= window:
        reason = f"command:{command} age={recent_age:.1f}s"

    if not reason and _bool(params.get("layout_check"), True):
        layout = image_layout(probe.data)
        if layout:
            min_ratio = _number(params.get("min_ratio"), 2.2, minimum=0)
            max_ratio = _number(params.get("max_ratio"), 4.5, minimum=0)
            dark = _number(params.get("right_dark"), 80.0, minimum=0)
            white_ratio = _number(params.get("right_white_min"), 0.0005, minimum=0)
            left_gap = _number(params.get("left_gap"), 25.0, minimum=0)
            if (
                min_ratio <= layout["ratio"] <= max_ratio
                and layout["right_mean"] <= dark
                and layout["right_white"] >= white_ratio
                and layout["left_mean"] >= layout["right_mean"] + left_gap
            ):
                reason = (
                    "layout:"
                    f"{layout['width']}x{layout['height']}"
                    f" ratio={layout['ratio']:.2f}"
                    f" right_mean={layout['right_mean']}"
                    f" right_white={layout['right_white']}"
                )

    if not reason:
        return None
    return FilterHit(
        filter_id="phoebe_bullshit",
        label=str(params.get("label") or "菲比暴论"),
        probe=probe,
        detail=reason,
    )


# ══════════════════════════════════════════════════════════
# 内置过滤：NSFW（可选依赖 onnxruntime）
# ══════════════════════════════════════════════════════════
_NSFW_IMG_SIZE = 224
_NSFW_MEAN = 0.5
_NSFW_STD = 0.5
_NSFW_LOCK = threading.Lock()
_NSFW_CACHE: dict[tuple[str, int], Any] = {}
_NSFW_WARNED: set[str] = set()


def _resolve_providers(ort, use_gpu: bool) -> list[str]:
    """按可用性挑执行提供器：GPU 优先（DirectML / CUDA），否则 CPU。

    Windows 上 ``onnxruntime-directml`` 包提供 DmlExecutionProvider（N/A/Intel/
    NVIDIA 显卡通吃，不需要装 CUDA）；``onnxruntime-gpu`` 提供 CUDAExecutionProvider
    （要装 CUDA + cuDNN 运行库）。两者都没装就回退 CPU，并打一次提示。
    """
    if not use_gpu:
        return ["CPUExecutionProvider"]
    available = ort.get_available_providers()
    for name in ("DmlExecutionProvider", "CUDAExecutionProvider"):
        if name in available:
            return [name, "CPUExecutionProvider"]
    if "gpu_no_provider" not in _NSFW_WARNED:
        _NSFW_WARNED.add("gpu_no_provider")
        log.warning(
            "[图片过滤] 配了 use_gpu 但没有可用的 GPU 执行器（已装: %s）；"
            "要开 GPU 请 pip install onnxruntime-directml，本次按 CPU 跑",
            available,
        )
    return ["CPUExecutionProvider"]


class _NsfwScorer:
    """ViT NSFW ONNX 推理，逻辑对齐 ``D:\\Code\\sex_filter\\nsfw_filter.py``。

    只依赖 onnxruntime / numpy / Pillow；模型不存在或依赖缺失时由调用方降级。
    ``use_gpu=True`` 时按可用性挑 GPU 执行器（Windows 首选 DirectML，其次 CUDA），
    加载失败自动回退 CPU —— GPU 崩溃有子进程隔离兜着，不影响 Bot。
    """

    def __init__(
        self, model_path: str, threads: int = 4,
        confirm_model_path: str = "", confirm_sat_max: float = 0.08,
        use_gpu: bool = False,
    ) -> None:
        import numpy as np
        import onnxruntime as ort

        self.np = np
        self.provider = _resolve_providers(ort, use_gpu)
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(threads))
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3
        try:
            self.session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=self.provider
            )
        except Exception:
            if self.provider == ["CPUExecutionProvider"]:
                raise  # CPU 都加载不了，没得回退
            log.warning(
                "[图片过滤] GPU 推理器 %s 加载模型失败，回退 CPU",
                self.provider[0],
            )
            self.provider = ["CPUExecutionProvider"]
            self.session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=self.provider
            )
        self.input_name = self.session.get_inputs()[0].name
        names = [output.name for output in self.session.get_outputs()]
        self.output_name = "logits" if "logits" in names else names[0]
        # 二次元复核模型（nsfwjs 5 分类）：黑白漫画/截图是 ViT 的误报重灾区，
        # 低饱和图 ViT 命中后用它复核（hentai+porn > drawings 才维持拦截）
        self.confirm_session = None
        self.confirm_sat_max = max(0.0, float(confirm_sat_max))
        # None = 未初始化（子进程 worker 构造时不带，首次判定从 params 懒读）
        self._confirm_model_path = confirm_model_path
        self._nsfwjs_labels = ["drawings", "hentai", "neutral", "porn", "sexy"]

    def _confirm(self):
        """懒加载 nsfwjs 复核模型；没配路径或加载失败返回 None。"""
        if not self._confirm_model_path:
            return None
        if self.confirm_session is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.log_severity_level = 3
            try:
                self.confirm_session = ort.InferenceSession(
                    str(self._confirm_model_path),
                    sess_options=options,
                    providers=self.provider,
                )
            except Exception:
                if self.provider == ["CPUExecutionProvider"]:
                    raise
                log.warning("[图片过滤] nsfwjs 复核模型 GPU 加载失败，回退 CPU")
                self.provider = ["CPUExecutionProvider"]
                self.confirm_session = ort.InferenceSession(
                    str(self._confirm_model_path),
                    sess_options=options,
                    providers=self.provider,
                )
        return self.confirm_session

    def _confirm_classify(self, image) -> dict:
        """nsfwjs 5 分类（drawings/hentai/neutral/porn/sexy），输入 224×224。"""
        import io as _io

        arr = image.resize((224, 224), Image.NEAREST)
        batch = (self.np.asarray(arr, dtype=self.np.float32) / 255.0)[None, ...]
        session = self._confirm()
        name = session.get_inputs()[0].name
        out = session.get_outputs()[0].name
        raw, = session.run([out], {name: batch})
        return dict(zip(self._nsfwjs_labels, map(float, raw[0])))

    def _saturation(self, image) -> float:
        """平均饱和度（0~1）：黑白漫画 / 文字截图都极低。"""
        hsv = image.convert("HSV")
        hsv.thumbnail((200, 200))
        return float(self.np.asarray(hsv, dtype=self.np.float32)[:, :, 1].mean() / 255.0)

    def _to_array(self, image):
        arr = self.np.asarray(image.convert("RGB"), dtype=self.np.float32) / 255.0
        arr = (arr - _NSFW_MEAN) / _NSFW_STD
        return arr.transpose(2, 0, 1)[None, ...]

    def _squash(self, image):
        return self._to_array(
            image.resize((_NSFW_IMG_SIZE, _NSFW_IMG_SIZE), Image.BICUBIC)
        )

    def _center_crop(self, image):
        width, height = image.size
        short = min(width, height)
        if short <= _NSFW_IMG_SIZE:
            return None
        scale = _NSFW_IMG_SIZE / short
        resized = image.resize(
            (max(_NSFW_IMG_SIZE, round(width * scale)),
             max(_NSFW_IMG_SIZE, round(height * scale))),
            Image.BICUBIC,
        )
        cw, ch = resized.size
        left, top = (cw - _NSFW_IMG_SIZE) // 2, (ch - _NSFW_IMG_SIZE) // 2
        return self._to_array(
            resized.crop((left, top, left + _NSFW_IMG_SIZE, top + _NSFW_IMG_SIZE))
        )

    def _tiles(self, image, grid: int, min_side: int):
        if grid < 2 or max(image.size) < min_side:
            return []
        width, height = image.size
        tiles = []
        for row in range(grid):
            for col in range(grid):
                box = (
                    col * width // grid,
                    row * height // grid,
                    (col + 1) * width // grid,
                    (row + 1) * height // grid,
                )
                tile = image.crop(box)
                if min(tile.size) < 64:
                    continue
                tiles.append((f"tile{row}{col}", self._squash(tile)))
        return tiles

    def _probs(self, batch):
        logits = self.np.asarray(
            self.session.run([self.output_name], {self.input_name: batch})[0],
            dtype=self.np.float32,
        )
        if logits.ndim == 1:
            logits = logits[None, :]
        shifted = logits - logits.max(axis=1, keepdims=True)
        exp = self.np.exp(shifted)
        return (exp / exp.sum(axis=1, keepdims=True))[:, -1]

    def score_bytes(self, data: bytes, params: dict) -> dict:
        from PIL import ImageFile

        # 截断图（部分下载的 GIF 第一帧 / JPEG 上半部）也能打分；
        # 完整图不受影响。子进程里也要设（主进程的开关带不过来）。
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(io.BytesIO(data)) as raw:
            image = ImageOps.exif_transpose(raw)
            if image.mode in ("P", "LA", "RGBA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            width, height = image.size

        views = [("full", self._squash(image))]
        crop = self._center_crop(image)
        if crop is not None:
            views.append(("crop", crop))
        probs = self._probs(self.np.concatenate([view[1] for view in views], axis=0))
        per_view = {tag: float(p) for (tag, _), p in zip(views, probs)}
        score = max(per_view.values())

        # 可疑区间才加跑分块；和 sex_filter 的默认行为一致（deep 默认关）
        threshold = _number(params.get("threshold"), 0.5, minimum=0)
        review_low = _number(params.get("review_low"), 0.05, minimum=0)
        if _bool(params.get("deep"), False) and review_low <= score < threshold:
            tiles = self._tiles(
                image,
                _integer(params.get("tile_grid"), 2, minimum=2),
                _integer(params.get("tile_min_side"), 512, minimum=0),
            )
            if tiles:
                tile_probs = self._probs(
                    self.np.concatenate([tile[1] for tile in tiles], axis=0)
                )
                per_view.update(
                    {tag: float(p) for (tag, _), p in zip(tiles, tile_probs)}
                )
                values = sorted(tile_probs)
                score = max(score, float(values[-2] if len(values) > 1 else values[0]))

        if score >= threshold:
            verdict = "nsfw"
        elif score >= review_low:
            verdict = "review"
        else:
            verdict = "ok"

        result = {
            "score": round(score, 4),
            "verdict": verdict,
            "width": width,
            "height": height,
            "views": {key: round(value, 4) for key, value in per_view.items()},
            "worst_view": max(per_view, key=per_view.get),
        }

        # 低饱和图（黑白漫画 / 文字截图）是 ViT 的误报重灾区：
        # 命中后用 nsfwjs 5 分类复核，drawings 主导（hentai+porn ≤ drawings）
        # 就改判「黑白图待人工」——发审计群让人确认，不自动拦截。
        if verdict == "nsfw" and self._confirm_model_path is not None:
            # 子进程 worker 构造时不带 confirm 参数：首次判定时从 params 懒初始化
            if not self._confirm_model_path:
                self._confirm_model_path = str(params.get("confirm_model_path") or "").strip()
                self.confirm_sat_max = max(0.0, float(params.get("confirm_sat_max") or 0.08))
            if self._confirm_model_path:
                try:
                    sat = self._saturation(image)
                    if sat < self.confirm_sat_max:
                        labels = self._confirm_classify(image)
                        hp = labels["hentai"] + labels["porn"]
                        result["confirm"] = {
                            "sat": round(sat, 3),
                            "nsfwjs": {k: round(v, 3) for k, v in labels.items()},
                            "released": hp <= labels["drawings"],
                        }
                except Exception as exc:
                    log.warning("[图片过滤] nsfwjs 复核失败（维持原判定）: %s", exc)
        return result


# ── 子进程隔离 ──────────────────────────────────────────────
# onnxruntime 推理是原生代码：在主进程里跑，偶发的 access violation 会把整个
# Bot 秒杀（无 traceback、无任何 Python 层兜底机会——线上真实发生过）。
# 所以默认把「模型加载 + 推理」全部放进一个独立子进程，主进程只做框架读写；
# 子进程崩溃只损失一次打分（安静跳过），Bot 不受影响。
# params 里 "subprocess": false 可退回老的主进程推理（调试用）。
_NSFW_WORKER_SRC = r'''
import json, struct, sys


def _read_frame(fp):
    header = fp.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    payload = bytearray()
    while len(payload) < length:
        chunk = fp.read(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    return bytes(payload)


def _write_frame(fp, payload: bytes) -> None:
    fp.write(struct.pack(">I", len(payload)))
    fp.write(payload)
    fp.flush()


def main():
    root, model_path, threads = sys.argv[1], sys.argv[2], int(sys.argv[3])
    use_gpu = len(sys.argv) > 4 and sys.argv[4] == "1"
    if root not in sys.path:
        sys.path.insert(0, root)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        from messaging.image_filter import _NsfwScorer
        scorer = _NsfwScorer(model_path, threads=threads, use_gpu=use_gpu)
    except BaseException as exc:  # 模型 / 依赖问题：报告后退出，不进循环
        _write_frame(stdout, json.dumps({"ready": False, "error": str(exc)}).encode("utf-8"))
        return
    _write_frame(stdout, json.dumps({"ready": True, "provider": scorer.provider}).encode("utf-8"))
    while True:
        head = _read_frame(stdin)
        if head is None:
            break
        data = _read_frame(stdin)
        if data is None:
            break
        try:
            params = json.loads(head.decode("utf-8"))
            result = scorer.score_bytes(data, params)
            _write_frame(stdout, json.dumps({"ok": True, "result": result}).encode("utf-8"))
        except BaseException as exc:
            _write_frame(stdout, json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"))


if __name__ == "__main__":
    main()
'''


def _read_frame(fp):
    """读一帧（4 字节大端长度 + 载荷）；流结束返回 None。"""
    import struct

    header = fp.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    payload = bytearray()
    while len(payload) < length:
        chunk = fp.read(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    return bytes(payload)


def _write_frame(fp, payload: bytes) -> None:
    import struct

    fp.write(struct.pack(">I", len(payload)))
    fp.write(payload)
    fp.flush()


def _drain_frames(fp, out) -> None:
    """后台线程：把子进程 stdout 的帧源源不断搬进队列；EOF 放入 None 哨兵。"""
    while True:
        try:
            frame = _read_frame(fp)
        except Exception:
            out.put(None)
            return
        out.put(frame)
        if frame is None:
            return


class _NsfwSubprocessScorer:
    """和 ``_NsfwScorer`` 同接口，但推理在独立子进程里执行。

    * 子进程原生崩溃 → 本次打分抛 RuntimeError（调用方安静跳过），下次调用自动重开
    * 连续崩 3 次 → 标记 ``_dead``，NSFW 过滤在本进程生命周期内停用（打一次日志）
    """

    _MAX_CONSECUTIVE_CRASHES = 3

    def __init__(self, model_path: str, threads: int = 4, use_gpu: bool = False) -> None:
        self.model_path = str(model_path)
        self.threads = max(1, int(threads))
        self.use_gpu = bool(use_gpu)
        from pathlib import Path

        self._root = str(Path(__file__).resolve().parents[1])
        self._proc = None
        self._queue = None
        self._dead = False
        self._consecutive_crashes = 0

    # ── 生命周期 ──
    def _start(self) -> bool:
        import queue
        import subprocess
        import sys
        import threading

        try:
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", _NSFW_WORKER_SRC,
                 self._root, self.model_path, str(self.threads),
                 "1" if self.use_gpu else "0"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            log.warning("[图片过滤] NSFW 推理子进程启动失败（%s），本次跳过", exc)
            return False
        frames: "queue.Queue" = queue.Queue()
        threading.Thread(
            target=_drain_frames, args=(proc.stdout, frames),
            daemon=True, name="nsfw-frame-reader",
        ).start()
        self._proc, self._queue = proc, frames
        message = self._next_message(timeout=120.0)   # 模型加载留足时间
        if not message or not message.get("ready"):
            reason = (message or {}).get("error") or "模型加载超时或子进程崩溃"
            log.warning(
                "[图片过滤] NSFW 推理子进程不可用（%s），本次跳过",
                reason,
            )
            self._teardown()
            return False
        self.provider = str(message.get("provider") or "CPUExecutionProvider")
        return True

    def _teardown(self) -> None:
        proc, self._proc, self._queue = self._proc, None, None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _next_message(self, timeout: float):
        """取下一帧并解析成 dict；超时 / 子进程退出 / 解析失败都返回 None。"""
        import json
        import queue as queue_mod

        try:
            frame = self._queue.get(timeout=timeout)
        except (queue_mod.Empty, ValueError, AttributeError):
            return None
        if frame is None:
            return None
        try:
            return json.loads(frame.decode("utf-8"))
        except Exception:
            return None

    def _fail(self, reason: str, timeout: bool) -> None:
        proc = self._proc
        exitcode = None
        if proc is not None:
            try:
                # 崩溃场景进程已死，wait 立刻返回收尸；真超时则等 2s 放弃
                proc.wait(timeout=2)
            except Exception:
                pass
            exitcode = proc.returncode
        if exitcode is not None:
            timeout = False   # 进程已经死了，是崩溃不是超时
        self._teardown()
        self._consecutive_crashes += 1
        if self._consecutive_crashes >= self._MAX_CONSECUTIVE_CRASHES:
            self._dead = True
            log.error(
                "[图片过滤] NSFW 推理子进程连续崩溃 %d 次（exitcode=%s），"
                "NSFW 过滤停用直到重启 Bot；其余过滤不受影响",
                self._MAX_CONSECUTIVE_CRASHES,
                exitcode,
            )
        raise RuntimeError(
            f"推理子进程{'超时' if timeout else '崩溃'}（{reason}，exitcode={exitcode}）"
        )

    # ── 对外接口（对齐 _NsfwScorer.score_bytes） ──
    def score_bytes(self, data: bytes, params: dict) -> dict:
        if self._dead:
            raise RuntimeError("NSFW 推理子进程已停用（连续崩溃），重启 Bot 恢复")
        if self._proc is None and not self._start():
            raise RuntimeError("NSFW 推理子进程启动失败")
        import json

        proc = self._proc
        try:
            _write_frame(
                proc.stdin, json.dumps(params, ensure_ascii=False).encode("utf-8")
            )
            _write_frame(proc.stdin, data)
        except (OSError, ValueError) as exc:
            self._fail(str(exc), timeout=False)
        reply = self._next_message(timeout=120.0)
        if reply is None:
            self._fail("等待推理结果超时或子进程退出", timeout=True)
        if not isinstance(reply, dict) or not reply.get("ok"):
            # 业务级错误（图片解码失败之类）：进程还活着，不算崩溃，不计数
            detail = reply.get("error") if isinstance(reply, dict) else "响应格式异常"
            raise RuntimeError(str(detail or "推理失败"))
        self._consecutive_crashes = 0
        return reply["result"]

    def shutdown(self) -> None:
        """关闭常驻推理子进程；重复调用安全。"""
        self._teardown()


def _get_nsfw_scorer(
    model_path: str, threads: int, use_subprocess: bool = True,
    use_gpu: bool = False,
):
    if not IMAGE_AVAILABLE:
        return None
    key = (str(model_path), int(threads), bool(use_subprocess), bool(use_gpu))
    with _NSFW_LOCK:
        if key in _NSFW_CACHE:
            return _NSFW_CACHE[key]
    try:
        import importlib.util
        from pathlib import Path

        if importlib.util.find_spec("onnxruntime") is None:
            raise RuntimeError("onnxruntime 未安装")
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"模型不存在: {model_path}")
        if use_subprocess:
            scorer = _NsfwSubprocessScorer(model_path, threads=threads, use_gpu=use_gpu)
        else:
            scorer = _NsfwScorer(model_path, threads=threads, use_gpu=use_gpu)
    except Exception as exc:
        if key not in _NSFW_WARNED:
            _NSFW_WARNED.add(key)
            log.warning("[图片过滤] NSFW 不可用（%s），该过滤保持停用", exc)
        scorer = None
    with _NSFW_LOCK:
        _NSFW_CACHE[key] = scorer
    return scorer


def match_nsfw(probe: ImageProbe, params: dict) -> FilterHit | None:
    if not probe.data:
        return None
    import os

    model_path = str(
        params.get("model_path")
        or os.environ.get("NSFW_MODEL_PATH")
        or ""
    ).strip()
    if not model_path:
        return None
    threads = _integer(params.get("threads"), 4, minimum=1)
    scorer = _get_nsfw_scorer(
        model_path, threads, _bool(params.get("subprocess"), True),
        _bool(params.get("use_gpu"), False),
    )
    if scorer is None or getattr(scorer, "_dead", False):
        return None
    try:
        with _NSFW_LOCK:
            result = scorer.score_bytes(probe.data, params)
    except Exception as exc:
        log.warning("[图片过滤] NSFW 推理失败: %s", exc)
        return None
    threshold = _number(params.get("threshold"), 0.5, minimum=0)
    if float(result.get("score") or 0) < threshold:
        return None
    confirm = result.get("confirm") or {}
    if confirm.get("released"):
        # 低饱和图 nsfwjs 复核放行：发审计群人工确认，不自动拦截
        labels = confirm.get("nsfwjs") or {}
        return FilterHit(
            filter_id="nsfw",
            label=str(params.get("manual_label") or "黑白图待人工"),
            probe=probe,
            score=float(result["score"]),
            detail=(
                f"复核放行 sat={confirm.get('sat')} "
                f"hentai={labels.get('hentai')} drawings={labels.get('drawings')}"
            ),
            manual=True,
        )
    return FilterHit(
        filter_id="nsfw",
        label=str(params.get("label") or "色图"),
        probe=probe,
        score=float(result["score"]),
        detail=(
            f"score={result['score']} verdict={result['verdict']}"
            f" worst={result['worst_view']}"
        ),
    )


def nsfw_score_bytes(data: bytes, params: dict) -> dict | None:
    """NSFW 原始打分（不做阈值判定），给测试 / 校准类命令展示用。

    模型不可用（没配 ``model_path`` / onnxruntime 未装 / 模型文件缺失）时
    安静返回 ``None``，和 :func:`match_nsfw` 的降级行为一致。
    """
    if not data:
        return None
    import os

    model_path = str(
        params.get("model_path")
        or os.environ.get("NSFW_MODEL_PATH")
        or ""
    ).strip()
    if not model_path:
        return None
    scorer = _get_nsfw_scorer(
        model_path,
        _integer(params.get("threads"), 4, minimum=1),
        _bool(params.get("subprocess"), True),
        _bool(params.get("use_gpu"), False),
    )
    if scorer is None or getattr(scorer, "_dead", False):
        return None
    try:
        with _NSFW_LOCK:
            return scorer.score_bytes(data, params)
    except Exception as exc:
        log.warning("[图片过滤] NSFW 推理失败: %s", exc)
        return None

def close_nsfw_scorers() -> None:
    """关闭缓存中的 NSFW 推理资源，供 Bot 优雅停机调用。"""
    with _NSFW_LOCK:
        scorers = list(_NSFW_CACHE.values())
        _NSFW_CACHE.clear()
    for scorer in scorers:
        shutdown = getattr(scorer, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════
# 内置过滤：图片哈希黑名单（以后手工加「就这张图」的规则）
# ══════════════════════════════════════════════════════════
def match_image_hash_blacklist(probe: ImageProbe, params: dict) -> FilterHit | None:
    if not probe.data:
        return None
    hashes = [str(item).strip().lower() for item in (params.get("hashes") or [])]
    hashes = [item for item in hashes if len(item) == 32]
    if not hashes:
        return None
    try:
        from media.image_hash import hash_distance, image_hash
    except Exception:
        return None
    current = image_hash(probe.data)
    if not current:
        return None
    max_distance = _integer(params.get("max_distance"), 8, minimum=0)
    best = min(hashes, key=lambda item: hash_distance(current, item))
    distance = hash_distance(current, best)
    if distance > max_distance:
        return None
    return FilterHit(
        filter_id="image_hash_blacklist",
        label=str(params.get("label") or "黑名单图片"),
        probe=probe,
        score=float(distance),
        detail=f"hash={current} nearest={best} distance={distance}",
    )


def default_registry() -> FilterRegistry:
    registry = FilterRegistry()
    registry.register(
        FilterSpec(
            "phoebe_bullshit",
            "菲比暴论",
            match_phoebe_bullshit,
            default_enabled=False,
            description="菲比大人触发的暴论图（上下文 + 左图右黑底白字版式）",
        )
    )
    registry.register(
        FilterSpec(
            "nsfw",
            "色图",
            match_nsfw,
            default_enabled=False,
            description="ViT NSFW ONNX 检测（可选依赖 onnxruntime）",
        )
    )
    registry.register(
        FilterSpec(
            "image_hash_blacklist",
            "黑名单图片",
            match_image_hash_blacklist,
            default_enabled=False,
            description="aHash + dHash 命中配置里的哈希列表",
        )
    )
    return registry


# ══════════════════════════════════════════════════════════
# 下载 + 判定入口
# ══════════════════════════════════════════════════════════
def _download_default(url: str, timeout: float = _DEFAULT_IMAGE_TIMEOUT) -> bytes | None:
    from messaging.forward_filter import download_image

    return download_image(url, timeout)


def _download_default_ex(
    url: str, timeout: float = _DEFAULT_IMAGE_TIMEOUT, max_bytes: int | None = None
) -> tuple[bytes | None, str]:
    """带失败原因的默认下载器（过滤判定专用）。

    注意：不要用「只下载头部」的截断图做判定——实测 GIF 截断解码出的画面
    和原图内容完全对不上（色图 GIF 部分图打分 ~0.001，原图 0.97+），会漏判。
    要判大图就全量下载（调大 max_bytes）。
    """
    from messaging.forward_filter import download_image_ex

    return download_image_ex(url, timeout, max_bytes=max_bytes)


# ── 本地图片缓存 / NapCat 代下载 ────────────────────────────
# QQ 协议端收图时会落本地缓存：``Tencent Files/<uin>/nt_qq/nt_data/Pic/<月>/Ori|Thumb``，
# 文件名就是图片 md5（image 段的 file 字段）。Thumb 里的 ``<md5>_720`` 是
# 720px 高清预览（1086×1448 之类），对 224px 的 NSFW 判定和版式判定绰绰有余。
# 嵌套聊天记录里的图协议端没有缓存（没真正收过），那种还是得走网络下载。
def _pic_cache_roots() -> list:
    """本地 Pic 缓存的所有候选根目录（按优先级）。"""
    from pathlib import Path

    base = Path.home() / "Documents" / "Tencent Files"
    if not base.is_dir():
        return []
    import time as _time

    now = _time.localtime()
    months = {_time.strftime("%Y-%m", now)}
    # 上个月也扫（跨月消息）
    import datetime as _dt

    prev = (_dt.date.today() - _dt.timedelta(days=20)).strftime("%Y-%m")
    months.add(prev)
    roots: list = []
    for uin_dir in sorted(base.glob("*")):
        for month in sorted(months, reverse=True):
            pic = uin_dir / "nt_qq" / "nt_data" / "Pic" / month
            if pic.is_dir():
                roots.append(pic)
    return roots


def _local_pic_bytes(file_id: str, max_bytes: int | None = None) -> bytes | None:
    """按图片 md5 在本机 QQ 缓存里找图；找到直接读文件，零网络开销。

    :param file_id: image 段的 ``file`` 字段（``<MD5>.<ext>``）。
    查找顺序：各号 Ori 原图 → Thumb ``_720``（高清预览）→ Thumb ``_0``（小图）
    → NapCat temp（``download_file`` 的落点）。
    """
    import os
    from pathlib import Path

    stem = str(file_id or "").strip()
    if not stem or "." not in stem:
        return None
    md5 = stem.split(".")[0].lower()
    limit = max_bytes if (max_bytes and max_bytes > 0) else 8 * 1024 * 1024
    patterns = [f"{md5}.*", f"{md5}_720.*", f"{md5}_0.*"]
    for pic_dir in _pic_cache_roots():
        for sub in ("Ori", "Thumb", "ThumbTemp"):
            directory = pic_dir / sub
            if not directory.is_dir():
                continue
            for pattern in patterns:
                for candidate in directory.glob(pattern):
                    try:
                        if candidate.stat().st_size > limit:
                            continue
                        with open(candidate, "rb") as f:
                            return f.read()
                    except OSError:
                        continue
    # NapCat download_file 的落点：Tencent Files/NapCat/temp/<md5>
    napcat_temp = Path.home() / "Documents" / "Tencent Files" / "NapCat" / "temp"
    if napcat_temp.is_dir():
        for pattern in (md5, f"{md5}.*"):
            for candidate in napcat_temp.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_size <= limit:
                        with open(candidate, "rb") as f:
                            return f.read()
                except OSError:
                    continue
    return None


def _napcat_download_bytes(
    url: str, napcat_http: str, napcat_token: str, max_bytes: int | None = None
) -> bytes | None:
    """NapCat ``download_file`` 代下载：URL → NapCat 本地文件 → 读回来。

    Bot 直连失败（代理 / 网络抖动）时的兜底；NapCat 和图床都在腾讯侧，
    有时候它下得动我们下不动的图。
    """
    import os

    if not napcat_http or not url.startswith(("http://", "https://")):
        return None
    try:
        import requests

        headers = {"Authorization": f"Bearer {napcat_token}"} if napcat_token else {}
        resp = requests.post(
            f"{napcat_http.rstrip('/')}/download_file",
            json={"url": url},
            headers=headers,
            timeout=120,
        )
        body = resp.json()
        if str(body.get("status") or "") != "ok":
            return None
        path = (body.get("data") or {}).get("file") or ""
        if not path or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        limit = max_bytes if (max_bytes and max_bytes > 0) else 8 * 1024 * 1024
        if size > limit:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        log.warning("[图片过滤] NapCat 代下载失败: %s", exc)
        return None


def _sender_of(message: dict) -> tuple[str, str]:
    sender = (message or {}).get("sender") or {}
    sender_id = str(
        (message or {}).get("user_id") or sender.get("user_id") or ""
    ).strip()
    sender_name = str(sender.get("card") or sender.get("nickname") or "").strip()
    return sender_id, sender_name


def _probe_for_segment(
    segment: dict,
    *,
    message: dict,
    event: dict,
    source: str,
    index: int,
) -> ImageProbe:
    data = segment.get("data") or {}
    sender_id, sender_name = _sender_of(message)
    group_id = str(
        message.get("group_id") or event.get("group_id") or ""
    ).strip()
    return ImageProbe(
        url=str(data.get("url") or "").strip(),
        file_id=str(data.get("file") or "").strip(),
        file_size=int(data.get("file_size") or 0),
        source=source,
        event=event,
        message=message,
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=str(message.get("message_id") or event.get("message_id") or ""),
        text=message_text(message),
        index=index,
    )


def probes_from_event(event: dict, *, source: str = "single_image") -> list[ImageProbe]:
    event = event or {}
    return [
        _probe_for_segment(seg, message=event, event=event, source=source, index=index)
        for index, seg in enumerate(iter_photos(event))
    ]


def probes_from_messages(
    messages: Iterable[dict],
    *,
    event: dict | None = None,
    source: str = "chat_record",
    max_images: int = _DEFAULT_MAX_IMAGES,
) -> list[ImageProbe]:
    """把消息列表里的普通图片转成 probe。

    ``max_images <= 0`` 表示不设上限（默认）：宁可多判几张，也不能把没判过的图
    当成「安全」放过去——这正是以前大记录漏判的根因。
    """
    probes: list[ImageProbe] = []
    try:
        limit = int(max_images or 0)
    except (TypeError, ValueError):
        limit = 0
    for message_index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        for segment in iter_photos(message):
            if limit > 0 and len(probes) >= limit:
                return probes
            probes.append(
                _probe_for_segment(
                    segment,
                    message=message,
                    event=event or message,
                    source=source,
                    index=message_index,
                )
            )
    return probes


class ImageFilterHub:
    """图片过滤门的运行时：配置、注册表、最近命令、下载缓存都在这里。"""

    def __init__(
        self,
        *,
        settings=None,
        download: Callable[[str, float], bytes | None] | None = None,
        registry: FilterRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.download = download or _download_default
        self.registry = registry or default_registry()
        self.commands = RecentCommandTracker()
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._cache_limit = 128
        self._cache_lock = threading.Lock()

    # ── 配置 ──
    def _raw_config(self) -> Any:
        getter = getattr(self.settings, "get", None)
        if callable(getter):
            try:
                return getter("image_filter", None)
            except TypeError:
                pass
        if isinstance(self.settings, dict):
            return self.settings.get("image_filter")
        return None

    @property
    def config(self) -> dict:
        return normalize_filter_config(self._raw_config())

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enable"))

    def note_event(self, event) -> None:
        self.commands.note_event(event)

    # ── 缓存（供二维码 / 指纹 / 标签复用同一份字节） ──
    def _cache_get(self, url: str) -> bytes | None:
        if not url:
            return None
        with self._cache_lock:
            value = self._cache.get(url)
            if value is not None:
                self._cache.move_to_end(url)
            return value

    def _cache_put(self, url: str, data: bytes | None) -> None:
        if not url or not data:
            return
        with self._cache_lock:
            self._cache[url] = data
            self._cache.move_to_end(url)
            while len(self._cache) > self._cache_limit:
                self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def close(self) -> None:
        """释放图片门缓存和模型推理资源。"""
        self.clear_cache()
        close_nsfw_scorers()

    def load_image(self, url: str, timeout: float = _DEFAULT_IMAGE_TIMEOUT) -> bytes | None:
        """同步取图：先缓存后下载。给 ``build_forward_nodes`` 的 image_loader 用。"""
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        try:
            data = self.download(url, timeout)
        except Exception:
            data = None
        self._cache_put(url, data)
        return data

    async def download_probes(
        self, probes: list[ImageProbe], *, max_bytes: int | None = None
    ) -> None:
        """并发补齐一批 probe 的图片字节（公开版，测试类命令也用）。

        ``max_bytes``：本次下载的大小上限（缺省用配置的 8MB）。判定大 GIF
        的场景可以放宽（测试命令用 30MB），搬运去重路径保持默认。
        """
        await self._download_probes(probes, max_bytes=max_bytes)

    # ── 判定 ──
    def active_filters(self) -> list[tuple[FilterSpec, dict, dict]]:
        return self.registry.active(self.config)

    def evaluate(self, probes: Iterable[ImageProbe], *, source: str = "") -> list[FilterHit]:
        """同步判定一批图片；配置关着或没有启用的过滤函数时直接返回空。"""
        config = self.config
        if not config.get("enable"):
            return []
        active = self.registry.active(config)
        if not active:
            return []
        hits: list[FilterHit] = []
        recent_cache: dict[str, tuple[str, float]] = {}
        for probe in probes:
            if source and not probe.source:
                probe.source = source
            group_id = str(probe.group_id or "")
            if group_id not in recent_cache:
                recent_cache[group_id] = self.commands.recent(group_id)
            text, age = recent_cache[group_id]
            probe.context.setdefault("recent_text", text)
            probe.context.setdefault("recent_age", age)
            for spec, params, _entry in active:
                try:
                    hit = spec.match(probe, params)
                except Exception as exc:  # 单个过滤失败不能挡住其它过滤
                    log.warning(
                        "[图片过滤] %s 判定异常: %s",
                        spec.filter_id,
                        exc,
                    )
                    continue
                if hit is not None:
                    hits.append(hit)
        return hits

    async def screen_event(self, event: dict) -> list[FilterHit]:
        """判定单条事件里的图片（机器人发图路径）。"""
        if not self.enabled:
            return []
        probes = probes_from_event(event, source="single_image")
        if not probes:
            return []
        return await self._screen_probes(probes, source="single_image")

    #: 命中即停模式的每批张数（和下载并发一致，最坏多下载 3 张）
    _SHORT_CIRCUIT_BATCH = 4

    async def _screen_probes(
        self, probes: list[ImageProbe], *, source: str
    ) -> list[FilterHit]:
        """下载 + 判定一批图片。

        ``short_circuit`` 关（默认）：全量并发下载 → 逐张判定，每张图都有结论
        （审计标签要逐张展示）。

        ``short_circuit`` 开：按批流水线（每批 4 张：下完 → 判 → 命中即停）——
        拦截语义不变（有命中整条就拦），但命中后后面的图一个字节都不用下载。
        """
        if not self.config.get("short_circuit"):
            await self._download_probes(probes)
            return await asyncio.to_thread(self.evaluate, probes, source=source)
        batch_size = self._SHORT_CIRCUIT_BATCH
        for start in range(0, len(probes), batch_size):
            batch = probes[start:start + batch_size]
            await self._download_probes(batch)
            hits = await asyncio.to_thread(self.evaluate, batch, source=source)
            if hits:
                return hits
        return []

    async def screen_messages(
        self,
        messages: Iterable[dict],
        *,
        event: dict | None = None,
        source: str = "chat_record",
    ) -> list[FilterHit]:
        """判定聊天记录里的图片。返回的 hit 里带 ``probe.message`` 原消息。

        ``messages`` 必须是**摊平后的叶子消息**（``ForwardExpander`` 的输出）。
        传单层 ``get_forward_msg`` 结果时，SnowLuma 的嵌套记录里只有内层
        ``forward id``，一张图都看不到——过滤会静默「全部放行」。
        """
        if not self.enabled:
            return []
        config = self.config
        if not config.get("scan_all_images"):
            return []
        probes = probes_from_messages(
            messages,
            event=event,
            source=source,
            max_images=int(config.get("max_images") or 0),
        )
        if not probes:
            return []
        return await self._screen_probes(probes, source=source)

    async def _download_probes(
        self, probes: list[ImageProbe], *, max_bytes: int | None = None
    ) -> None:
        config = self.config
        timeout = float(config.get("image_timeout") or _DEFAULT_IMAGE_TIMEOUT)
        if max_bytes is None:
            configured = int(config.get("max_image_bytes") or 0)
            max_bytes = configured if configured > 0 else _DEFAULT_MAX_IMAGE_BYTES
        semaphore = asyncio.Semaphore(4)
        default_downloader = self.download is _download_default
        napcat_http = str(self.config.get("napcat_http") or "").strip()
        napcat_token = str(self.config.get("napcat_token") or "").strip()

        async def one(probe: ImageProbe) -> None:
            if probe.data is not None or not probe.url:
                return
            cached = self._cache_get(probe.url)
            if cached is not None:
                probe.data = cached
                return
            limit = max_bytes if (max_bytes and max_bytes > 0) else _DEFAULT_MAX_IMAGE_BYTES
            data, reason = None, ""
            async with semaphore:
                # ① 本地磁盘缓存（各号 Ori/Thumb + NapCat temp，按 md5）：
                #    零网络开销，Thumb 的 _720 高清预览对判定也够用
                #    （本地直读不花流量，file_size 超限也照读）
                if default_downloader and probe.file_id:
                    data = await asyncio.to_thread(
                        _local_pic_bytes, probe.file_id, max_bytes
                    )
                # ② 段里自带文件大小（NapCat 有）→ 网络下载前就拦，不浪费流量
                if not data and probe.file_size and probe.file_size > limit:
                    probe.error = (
                        f"超过大小上限({probe.file_size // 1024 // 1024}MB>"
                        f"{limit // 1024 // 1024}MB，来源 file_size 预检)"
                    )
                    return
                # ③ 网络直连（ex 版带失败原因，挂到 probe.error 给报告用）
                if not data:
                    if default_downloader:
                        try:
                            data, reason = await asyncio.to_thread(
                                _download_default_ex, probe.url, timeout, max_bytes
                            )
                        except Exception as exc:
                            data, reason = None, f"下载异常: {exc}"
                    else:
                        try:
                            data = await asyncio.to_thread(
                                self.download, probe.url, timeout
                            )
                        except Exception:
                            data = None
                        reason = ""
                # ④ NapCat download_file 代下载兜底（直连挂了它常能下动）
                if not data and default_downloader and napcat_http:
                    data = await asyncio.to_thread(
                        _napcat_download_bytes,
                        probe.url, napcat_http, napcat_token, max_bytes,
                    )
                    reason = "" if data else (reason or "NapCat 代下载也未成功")
            if data:
                self._cache_put(probe.url, data)
                probe.data = data
            else:
                probe.error = reason

        await asyncio.gather(*(one(probe) for probe in probes))


__all__ = [
    "FilterHit",
    "FilterRegistry",
    "FilterSpec",
    "close_nsfw_scorers",
    "IMAGE_AVAILABLE",
    "ImageFilterHub",
    "ImageProbe",
    "RecentCommandTracker",
    "default_registry",
    "image_layout",
    "match_image_hash_blacklist",
    "match_nsfw",
    "match_phoebe_bullshit",
    "normalize_filter_config",
    "nsfw_score_bytes",
    "probes_from_event",
    "probes_from_messages",
]
