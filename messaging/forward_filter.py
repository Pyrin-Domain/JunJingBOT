"""合并转发的「末尾广告裁剪」与「尾部追加」——搬运链路的纯逻辑层。

群里的聊天记录（旧版 ``type=forward`` / 新版 ``com.tencent.multimsg``）经常在
末尾挂广告：带二维码的进群海报、群推荐卡片、或者干脆一条链接。这类东西
通常就在最后几条，所以这里只扫末尾 ``scan_tail`` 条，命中就整条丢掉。

裁剪掉之后原来那张卡片就不能再整卡转发了（``forward_group_single_msg`` 只能
原样搬运），必须把内容重建成一串 node 再 ``send_group_forward_msg``；顺便也就
能在末尾追加自己的签名消息（``append.items``，可以配多条文本 / 图片）。

本模块**不碰网络也不 import bot**：图片下载走可注入的 ``image_loader``，
二维码识别用 OpenCV（缺了就安静跳过 QR 判定，其他规则照常）。

常用入口::

    from messaging.forward_filter import build_forward_nodes, normalize_settings

    opts = normalize_settings(raw_config)          # 容忍 None / True / 旧写法
    nodes, report = build_forward_nodes(messages, opts)
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

try:  # OpenCV 缺失时只关掉二维码判定，其余规则不受影响
    import cv2  # type: ignore

    QR_AVAILABLE = True
except Exception:  # pragma: no cover - 环境没装 opencv
    cv2 = None  # type: ignore[assignment]
    QR_AVAILABLE = False

try:
    import requests
except Exception:  # pragma: no cover - 环境没装 requests
    requests = None  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════
# 默认值 / 特征表
# ══════════════════════════════════════════════════════════
DEFAULT_SCAN_TAIL = 3

# 群邀请 / 加群落地页。命中即认定为群推荐广告
GROUP_URL_HINTS = (
    "m.q.qq.com/a/p/",  # 群推荐卡片落地页（点我加入群聊）
    "qun.qq.com",
    "qq.com/q/",
    "api.q.qq.com",
    "wpa.qq.com/msgrd",
    "qun.qq.com/join",
)
# 文案特征：加群 / 扫码
GROUP_TEXT_HINTS = (
    "加入群聊",
    "点我加入",
    "点我传送",
    "扫码加入",
    "扫一扫",
    "进群",
    "加群",
    "加入我们",
)
# 明确的广告卡片 app（tuwen=图文/群推荐，groupbulletin=群公告，mannounce=群通知）
AD_APPS = frozenset(
    {
        "com.tencent.tuwen.lua",
        "com.tencent.groupbulletin",
        "com.tencent.mannounce",
        "com.tencent.qun.invite",
    }
)

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")

# 纯链接判定：把链接挖掉后剩下的字不超过这个数，就算「这条消息只是个链接」
MAX_LINK_ONLY_CHARS = 6

MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_IMAGE_TIMEOUT = 8.0

_QR_CACHE_MAX = 256
_QR_CACHE: "OrderedDict[str, bool]" = OrderedDict()


# ══════════════════════════════════════════════════════════
# 配置归一化
# ══════════════════════════════════════════════════════════
def normalize_settings(raw: Any) -> dict:
    """把配置里的 ``auto_forward.strip_tail_ads`` 归一成一个 dict。

    容忍几种写法：

    - 没配 / ``None`` → 返回空 dict（调用方据此判定「功能没开」）
    - ``True`` / ``False`` → 只开裁剪、其余用默认值
    - dict → 逐项取默认值，缺的补齐
    """
    if raw is None or raw is False:
        return {}
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        return {}
    opts = {
        "enable": bool(raw.get("enable", True)),
        "scan_tail": _as_int(raw.get("scan_tail"), DEFAULT_SCAN_TAIL, minimum=0),
        "drop_qr": _bool(raw.get("drop_qr"), True),
        "drop_group_card": _bool(raw.get("drop_group_card"), True),
        "drop_link": _bool(raw.get("drop_link"), True),
        "image_timeout": float(raw.get("image_timeout") or DEFAULT_IMAGE_TIMEOUT),
        "append": _normalize_append(raw.get("append")),
    }
    return opts


def _normalize_append(raw: Any) -> dict:
    """追加消息段：``{"enable": bool, "items": [...]}``。"""
    if raw is None:
        return {"enable": False, "items": []}
    if isinstance(raw, bool):
        return {"enable": raw, "items": []}
    if isinstance(raw, list):  # 手写配置允许直接给数组
        return {"enable": True, "items": [i for i in raw if isinstance(i, dict)]}
    if not isinstance(raw, dict):
        return {"enable": False, "items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "enable": bool(raw.get("enable", True)),
        "items": [item for item in items if isinstance(item, dict)],
    }


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None and result < minimum:
        result = default
    return result


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def is_enabled(opts: dict | None) -> bool:
    """这份配置是否值得为它重建转发（裁剪或追加任一开启）。"""
    if not opts:
        return False
    if not opts.get("enable", True):
        return False
    append = opts.get("append") or {}
    return bool(
        opts.get("scan_tail")
        and (opts.get("drop_qr") or opts.get("drop_group_card") or opts.get("drop_link"))
    ) or bool(append.get("enable") and append.get("items"))


# ══════════════════════════════════════════════════════════
# 消息段读取小工具
# ══════════════════════════════════════════════════════════
def iter_segments(msg: dict, seg_type: str | None = None):
    """遍历一条消息的段（``content`` 老写法也认）。"""
    segs = (msg or {}).get("message")
    if not segs:
        segs = (msg or {}).get("content") or []
    for seg in segs or []:
        if not isinstance(seg, dict):
            continue
        if seg_type is None or seg.get("type") == seg_type:
            yield seg


def _loads(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def iter_json_payloads(msg: dict):
    """yield 消息里 json 卡片解析出来的 dict。"""
    for seg in iter_segments(msg, "json"):
        payload = _loads((seg.get("data") or {}).get("data") or "")
        if isinstance(payload, dict):
            yield payload


def iter_xml(msg: dict):
    for seg in iter_segments(msg, "xml"):
        yield str((seg.get("data") or {}).get("data") or "")


def message_text(msg: dict) -> str:
    """只取 text 段的正文（不含 CQ / 卡片内容）。"""
    return "".join(
        str((seg.get("data") or {}).get("text") or "") for seg in iter_segments(msg, "text")
    )


def card_urls_and_text(msg: dict) -> tuple[list[str], str]:
    """从 json / xml 卡片里抠出全部链接与可读文案。"""
    urls: list[str] = []
    texts: list[str] = []
    for payload in iter_json_payloads(msg):
        texts.append(str(payload.get("prompt") or ""))
        meta = payload.get("meta")
        if isinstance(meta, dict):
            for node in meta.values():
                if not isinstance(node, dict):
                    continue
                for key in ("jumpUrl", "qqdocurl", "url", "preview"):
                    value = str(node.get(key) or "")
                    if value:
                        urls.append(value)
                for key in ("title", "desc"):
                    value = str(node.get(key) or "")
                    if value:
                        texts.append(value)
    for raw in iter_xml(msg):
        texts.append(raw)
        urls.extend(URL_RE.findall(raw))
    return urls, " ".join(texts)


# ══════════════════════════════════════════════════════════
# 逐条判定
# ══════════════════════════════════════════════════════════
def classify_message(
    msg: dict,
    *,
    drop_qr: bool = True,
    drop_group_card: bool = True,
    drop_link: bool = True,
    image_timeout: float = DEFAULT_IMAGE_TIMEOUT,
    image_loader: Callable[[str, float], bytes | None] | None = None,
) -> str | None:
    """判定一条（转发记录里的）消息是不是广告。

    :return: 丢弃原因 ``qr_image`` / ``group_card`` / ``plain_link``；该保留时返回 None
    """
    if drop_group_card:
        for payload in iter_json_payloads(msg):
            if str(payload.get("app") or "") in AD_APPS:
                return "group_card"
        urls, text = card_urls_and_text(msg)
        lowered = " ".join(urls).lower()
        if any(hint in lowered for hint in GROUP_URL_HINTS):
            return "group_card"
        if any(hint in text for hint in GROUP_TEXT_HINTS):
            # 只在卡片类消息上按文案判：纯聊天文本交给下面的纯链接规则，
            # 否则群里正常说一句「进群了吗」也会被当成广告
            if _iter_json_payloads_exist(msg) or list(iter_xml(msg)) or _is_media_first(msg):
                return "group_card"

    if drop_qr:
        for seg in iter_segments(msg, "image"):
            url = str((seg.get("data") or {}).get("url") or "")
            if image_has_qr(url, timeout=image_timeout, loader=image_loader):
                return "qr_image"

    if drop_link:
        body = message_text(msg).strip()
        if body:
            found = URL_RE.findall(body)
            if found:
                # 「只剩一条链接」才算广告：把链接挖掉后几乎没字了
                rest = URL_RE.sub("", body).strip()
                if len(rest) <= MAX_LINK_ONLY_CHARS:
                    return "plain_link"
                if any(hint in body.lower() for hint in GROUP_URL_HINTS):
                    return "plain_link"
    return None


def _iter_json_payloads_exist(msg: dict) -> bool:
    return next(iter_json_payloads(msg), None) is not None


def _is_media_first(msg: dict) -> bool:
    """第一条段不是文本（图片 / 卡片等）—— 用来把「卡片类」和纯聊天区分开。"""
    first = next(iter_segments(msg), None)
    return bool(first) and first.get("type") != "text"


def preview_of(msg: dict) -> str:
    """日志里用的一行摘要。"""
    body = message_text(msg).strip()
    if body:
        return body[:60]
    kinds = [seg.get("type") for seg in iter_segments(msg)]
    _, text = card_urls_and_text(msg)
    return f"<{','.join(k or '?' for k in kinds)}> {text[:50]}".strip()


# ══════════════════════════════════════════════════════════
# 二维码识别
# ══════════════════════════════════════════════════════════
def image_has_qr(
    url: str,
    *,
    timeout: float = DEFAULT_IMAGE_TIMEOUT,
    loader: Callable[[str, float], bytes | None] | None = None,
) -> bool:
    """图片里有没有二维码（能解出内容或至少检测到角点）。

    结果按 url 缓存：同一条转发被反复搬运时不重复下载。
    """
    if not url or cv2 is None:
        return False
    cached = _QR_CACHE.get(url)
    if cached is not None:
        _QR_CACHE.move_to_end(url)
        return cached
    data = (loader or download_image)(url, timeout)
    result = qr_in_bytes(data) if data else False
    _QR_CACHE[url] = result
    if len(_QR_CACHE) > _QR_CACHE_MAX:
        _QR_CACHE.popitem(last=False)
    return result


def qr_in_bytes(data: bytes | None) -> bool:
    """解码图片字节流并检测二维码。"""
    if not data or cv2 is None:
        return False
    try:
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        detector = cv2.QRCodeDetector()
        text, _, _ = detector.detectAndDecode(img)
        if text:
            return True
        ok, _ = detector.detect(img)
        return bool(ok)
    except Exception as exc:  # pragma: no cover - 解码失败不该影响搬运
        print(f"[转发裁剪] 二维码检测失败: {exc}")
        return False


def download_image(url: str, timeout: float = DEFAULT_IMAGE_TIMEOUT) -> bytes | None:
    """下载图片字节；超过 ``MAX_IMAGE_BYTES`` 或失败返回 None。

    先按环境（含系统代理）下载，失败再强制直连 —— 内网代理挂掉时 QQ 的
    图片域名往往仍然直连可达。
    """
    if requests is None or not url.startswith(("http://", "https://")):
        return None
    for proxies in (None, {"http": None, "https": None}):
        try:
            resp = requests.get(url, timeout=timeout, proxies=proxies, stream=True)
            if resp.status_code != 200:
                continue
            length = int(resp.headers.get("Content-Length") or 0)
            if length and length > MAX_IMAGE_BYTES:
                return None
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════
# 裁剪 + 重建
# ══════════════════════════════════════════════════════════
def strip_tail_ads(
    messages: list[dict],
    *,
    scan_tail: int = DEFAULT_SCAN_TAIL,
    drop_qr: bool = True,
    drop_group_card: bool = True,
    drop_link: bool = True,
    image_timeout: float = DEFAULT_IMAGE_TIMEOUT,
    image_loader: Callable[[str, float], bytes | None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """只看末尾 ``scan_tail`` 条，把广告整条丢掉。

    :return: ``(保留的消息, 丢弃记录)``；丢弃记录形如
        ``{"index": 31, "reason": "qr_image", "preview": ""}``
    """
    total = len(messages or [])
    start = max(0, total - max(0, scan_tail)) if scan_tail else total
    kept: list[dict] = []
    removed: list[dict] = []
    for index, msg in enumerate(messages or []):
        if index < start:
            kept.append(msg)
            continue
        reason = classify_message(
            msg,
            drop_qr=drop_qr,
            drop_group_card=drop_group_card,
            drop_link=drop_link,
            image_timeout=image_timeout,
            image_loader=image_loader,
        )
        if reason:
            removed.append({"index": index, "reason": reason, "preview": preview_of(msg)})
        else:
            kept.append(msg)
    return kept, removed


def build_nodes(messages: list[dict]) -> list[dict]:
    """把转发记录还原成 ``send_group_forward_msg`` 的 node 列表。"""
    nodes: list[dict] = []
    for msg in messages or []:
        sender = (msg or {}).get("sender") or {}
        segs = list(iter_segments(msg))
        if not segs:
            continue
        nodes.append(
            {
                "type": "node",
                "data": {
                    "uin": str(sender.get("user_id") or msg.get("user_id") or "0"),
                    "name": str(sender.get("card") or sender.get("nickname") or "QQ用户"),
                    "time": int(msg.get("time") or 0),
                    "content": segs,
                },
            }
        )
    return nodes


def build_append_nodes(items: list[dict], *, now: int | None = None) -> list[dict]:
    """配置里的追加条目 → node 列表，顺序即列表顺序。

    每个条目::

        {"uin": 10004, "name": "昵称", "content": "文本"}
        {"uin": 10004, "name": "昵称", "content": [{"type": "image", ...}]}

    ``content`` 是字符串时当文本；是段数组时原样用；本地图片路径自动内联成
    ``base64://``（NapCat 常常跑在 WSL / 容器里，读不到 Windows 路径）。
    """
    stamp = int(now if now is not None else time.time())
    nodes: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        segs = content_to_segments(item.get("content"))
        if not segs:
            continue
        nodes.append(
            {
                "type": "node",
                "data": {
                    "uin": str(item.get("uin") or item.get("user_id") or "0"),
                    "name": str(item.get("name") or item.get("nickname") or "QQ用户"),
                    "time": int(item.get("time") or stamp),
                    "content": segs,
                },
            }
        )
    return nodes


def content_to_segments(content: Any) -> list[dict]:
    """配置里的 content → 消息段数组。"""
    if isinstance(content, str):
        return [{"type": "text", "data": {"text": content}}] if content else []
    if isinstance(content, dict):
        segs = [content]
    elif isinstance(content, (list, tuple)):
        segs = [seg for seg in content if isinstance(seg, dict)]
    else:
        return []
    return [inline_local_image(seg) for seg in segs]


def inline_local_image(seg: dict) -> dict:
    """图片段的本地文件 → ``base64://``；已经是 url / base64 的原样返回。"""
    if not isinstance(seg, dict) or seg.get("type") != "image":
        return seg
    data = dict(seg.get("data") or {})
    ref = str(data.get("file") or "").strip()
    if not ref or ref.startswith(("http://", "https://", "base64://", "{base64://")):
        return seg
    # CQ 码风格的 file=xxx.jpg 也可能是 NapCat 侧已有缓存，不是本地路径就跳过
    path = Path(ref)
    try:
        if not path.is_file():
            return seg
        data["file"] = "base64://" + base64.b64encode(path.read_bytes()).decode()
    except Exception as exc:  # pragma: no cover - 读不到就交给上层报错
        print(f"[转发裁剪] 图片内联失败 {ref}: {exc}")
        return seg
    return {**seg, "data": data}


def build_forward_nodes(
    messages: list[dict],
    opts: dict,
    *,
    image_loader: Callable[[str, float], bytes | None] | None = None,
    now: int | None = None,
) -> tuple[list[dict], dict]:
    """裁剪末尾广告 + 追加自定义消息，产出一份可直接发送的 node 列表。

    :param opts: :func:`normalize_settings` 的结果
    :return: ``(nodes, report)``，report 里记了保留 / 丢弃 / 追加的条数，
        调用方拿它决定「要不要重建发送」，以及往日志里写什么
    """
    report: dict = {"total": len(messages or []), "removed": [], "appended": 0}
    kept = list(messages or [])
    if opts.get("scan_tail") and (
        opts.get("drop_qr") or opts.get("drop_group_card") or opts.get("drop_link")
    ):
        kept, removed = strip_tail_ads(
            messages,
            scan_tail=opts.get("scan_tail", DEFAULT_SCAN_TAIL),
            drop_qr=bool(opts.get("drop_qr", True)),
            drop_group_card=bool(opts.get("drop_group_card", True)),
            drop_link=bool(opts.get("drop_link", True)),
            image_timeout=float(opts.get("image_timeout") or DEFAULT_IMAGE_TIMEOUT),
            image_loader=image_loader,
        )
        report["removed"] = removed
    nodes = build_nodes(kept)
    report["kept"] = len(nodes)

    append = opts.get("append") or {}
    if append.get("enable") and append.get("items"):
        extra = build_append_nodes(append["items"], now=now)
        nodes.extend(extra)
        report["appended"] = len(extra)
    report["rebuilt"] = bool(report["removed"] or report["appended"])
    return nodes, report


__all__ = [
    "AD_APPS",
    "DEFAULT_SCAN_TAIL",
    "GROUP_TEXT_HINTS",
    "GROUP_URL_HINTS",
    "QR_AVAILABLE",
    "build_append_nodes",
    "build_forward_nodes",
    "build_nodes",
    "card_urls_and_text",
    "classify_message",
    "content_to_segments",
    "download_image",
    "image_has_qr",
    "inline_local_image",
    "is_enabled",
    "message_text",
    "normalize_settings",
    "preview_of",
    "qr_in_bytes",
    "strip_tail_ads",
]
