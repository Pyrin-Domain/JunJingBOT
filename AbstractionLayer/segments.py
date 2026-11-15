"""消息段语义归一（NapCat / SnowLuma 共用）。

抽象层原先只统一了**后端能力**（历史游标、分页上限、转发嵌套），但消息段
本身的语义两端也不一样 —— 最典型的就是「图片」和「表情」的区分。这里把差异
收在一处，业务层只问「这条消息里有没有真图片」。

实测（2026-10-31，两端各拉真实群历史逐条统计）：

========== =================================== =====================================
场景       SnowLuma                            NapCat
========== =================================== =====================================
普通图片   ``sub_type=0``、``summary="[图片]"``  ``sub_type=0``、``summary`` 也可能为空
收藏表情   ``sub_type=1``、``summary="[动画表情]"`` ``sub_type=1``、``summary="[动画表情]"``
商城表情   ``sub_type=7``、``summary="[emo]"``   **没有 ``sub_type`` 字段**，``summary``
                                               是表情名（``[唤马]`` / ``[玩石]``），
                                               另带 ``key`` / ``emoji_id`` /
                                               ``emoji_package_id``
========== =================================== =====================================

所以判据必须是三条，每条都对应一类真实样本，缺一条就会漏：

1. 带 ``emoji_id`` / ``emoji_package_id`` / ``key`` → 表情
   （NapCat 的商城表情，``sub_type`` 完全缺失，只能靠这三个字段认）；
2. ``sub_type`` 不是 0 → 表情
   （收藏表情 = 1；SnowLuma 商城表情 = 7，这类不带上述字段）；
3. ``summary`` 形如 ``[xxx]`` 且不是图片标记 → 表情
   （表情名直接顶在 ``summary`` 上，如 ``[呜呜]``、``[emo]``）。

反过来「判据太严」也踩过坑：早期用「``summary`` 为空才算图片」，而两端给普通
图片的 ``summary`` 都是 ``"[图片]"``，结果判据恒为假、图片搬运从来没触发过。
因此 ``summary`` 不是方括号形式时（个别端填文件名）仍然按图片处理。

已知取舍：闪照（``summary`` 形如 ``[闪照]``）会被算作表情 —— 这类图片本来
就不该被搬运，宁可漏也不误转。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlparse

#: 段的归类结果
PHOTO = "photo"
STICKER = "sticker"
OTHER = "other"

#: 普通图片在 ``summary`` 上的写法：空串或 ``"[图片]"``
_PHOTO_SUMMARIES = frozenset({"", "[图片]"})

#: 商城表情的标识字段（NapCat 官方文档：商城表情转成 image 段时附带这三个）
_STICKER_KEYS = ("emoji_id", "emoji_package_id", "key")

#: 表情名渲染时的最大长度（表情名是用户起的，偶尔是一整句话）
STICKER_NAME_MAX = 12


def _segments(message: Any) -> Iterable[dict]:
    """消息段列表；也接受 ``{"message": [...]}`` 这种事件对象。"""
    if isinstance(message, dict):
        message = message.get("message")
    if not isinstance(message, (list, tuple)):
        return ()
    return (seg for seg in message if isinstance(seg, dict))


def _sub_type(data: dict) -> int:
    try:
        return int(data.get("sub_type") or 0)
    except (TypeError, ValueError):
        return 0


def image_kind(segment: Any) -> str:
    """把一个段归成 :data:`PHOTO` / :data:`STICKER` / :data:`OTHER`。

    ``mface``（商城表情）一律算表情；``image`` 段再按三条判据细分。
    """
    if not isinstance(segment, dict):
        return OTHER

    seg_type = segment.get("type")
    if seg_type == "mface":
        return STICKER
    if seg_type != "image":
        return OTHER

    data = segment.get("data") or {}
    # ① NapCat 的商城表情：sub_type 缺失，只有表情标识字段
    if any(data.get(key) for key in _STICKER_KEYS):
        return STICKER
    # ② 收藏表情（1）/ SnowLuma 商城表情（7）
    if _sub_type(data) != 0:
        return STICKER

    summary = str(data.get("summary") or "").strip()
    if summary in _PHOTO_SUMMARIES:
        return PHOTO
    # ③ 形如 "[动画表情]" / "[呜呜]" 的是表情名
    if summary.startswith("[") and summary.endswith("]"):
        return STICKER
    # 个别协议端把 summary 填成文件名 —— 不能因此丢掉真图片
    return PHOTO


def is_photo(segment: Any) -> bool:
    """这个段是不是**普通图片**（表情不算）。"""
    return image_kind(segment) == PHOTO


def iter_photos(message: Any) -> Iterator[dict]:
    """挑出消息里的普通图片段。"""
    return (seg for seg in _segments(message) if image_kind(seg) == PHOTO)


#: 这些 ``summary`` 是「表情的统称」而不是名字，渲染时不该变成 ``[表情:动画表情]``。
_GENERIC_STICKER_NAMES = frozenset(
    {"", "动画表情", "表情", "表情包", "贴纸", "图片", "表情图片", "商城表情"}
)


def sticker_name(segment: Any) -> str:
    """表情的显示名（``[呜呜]`` → ``呜呜``）；没有有意义的名字时返回空串。

    为什么要区分：收藏表情的 ``summary`` 是统称 ``[动画表情]``，而商城表情
    （NapCat 的 ``[唤马]``、SnowLuma 的 ``[emo]``）带的是真名字。后者对理解
    上下文有帮助，前者只是噪音。

    长度截到 :data:`STICKER_NAME_MAX` 个字符：表情名是用户起的，偶尔会是一句话。
    """
    if image_kind(segment) != STICKER:
        return ""
    data = segment.get("data") or {}
    raw = str(data.get("summary") or "").strip()
    name = raw.strip("[]【】()（）").strip()
    if name in _GENERIC_STICKER_NAMES:
        return ""
    return name[:STICKER_NAME_MAX]


def has_photo(message: Any) -> bool:
    """消息里有没有普通图片——业务层判断「要不要搬运 / 识别」时用这个。"""
    return any(image_kind(seg) == PHOTO for seg in _segments(message))


def photo_urls(message: Any) -> list[str]:
    """普通图片的 URL 列表（表情的 URL 不该进 OCR / 图片哈希）。"""
    urls = []
    for seg in iter_photos(message):
        url = (seg.get("data") or {}).get("url")
        if url:
            urls.append(str(url))
    return urls


# ── 发送方向：file 段必须带 name ─────────────────────────────────
#
# 实测（2026-11-01，SnowLuma 2.2.1，同一个 30.2 MB PDF、同一个测试群）：
#
#   {"type":"file","data":{"file": pdf}}                  → retcode=100, code=-134
#   {"type":"file","data":{"file": pdf,"name":"a.pdf"}}   → retcode=0
#
# -134 在 QQ 的 UploadGroupFileRequest 阶段就返回，字节根本没开始传。NapCat
# 的 file 段本来就把 name 当可选显示名，多带一个不会改变行为；SnowLuma 则
# 只凭 file 构造请求，缺 name 会被 QQ 直接拒绝。所以「补 name」放在抽象层，
# 业务代码不用再记这个坑。

#: Windows 盘符路径（``C:\...`` / ``C:/...``）不是 URL scheme
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def file_name_from_source(source: Any) -> str:
    """从 file 段的来源推断显示文件名；推断不出时返回空串。

    支持本地路径（Windows / POSIX / UNC）、``file://`` URI 和 http(s) URL。
    ``base64://`` / ``data:`` 这类内联内容没有文件名，直接跳过。
    """
    if not isinstance(source, str):
        return ""
    text = source.strip()
    if not text or text.startswith(("base64://", "data:")):
        return ""

    if _DRIVE_PATH.match(text) or text.startswith("\\\\"):
        candidate = text
    else:
        parsed = urlparse(text)
        if parsed.scheme in ("http", "https", "file"):
            candidate = unquote(parsed.path)
        elif parsed.scheme and "://" in text:
            return ""  # 不认识的 scheme，别瞎猜
        else:
            candidate = text

    candidate = candidate.replace("\\", "/").rstrip("/")
    name = candidate.rsplit("/", 1)[-1]
    if not name or name in (".", "..") or name.endswith(":"):
        return ""
    return name


def normalize_outgoing(message: Any) -> Any:
    """给发送消息里的 ``file`` 段补上 ``name``，其余段原样返回。

    不修改调用方传入的 list / dict：有改动时返回新对象，没改动时原样返回，
    这样上层可以继续用 ``is`` 判断要不要复制。
    """
    if not isinstance(message, (list, tuple)):
        return message

    normalized: list[Any] = []
    changed = False
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "file":
            data = segment.get("data")
            if isinstance(data, dict) and not str(data.get("name") or "").strip():
                name = file_name_from_source(data.get("file"))
                if name:
                    segment = {**segment, "data": {**data, "name": name}}
                    changed = True
        normalized.append(segment)
    return normalized if changed else message


__all__ = [
    "OTHER",
    "PHOTO",
    "STICKER",
    "STICKER_NAME_MAX",
    "file_name_from_source",
    "has_photo",
    "image_kind",
    "is_photo",
    "iter_photos",
    "normalize_outgoing",
    "photo_urls",
    "sticker_name",
]
