"""合并转发的「广告 / 占位卡片清洗」与「尾部追加」——搬运链路的纯逻辑层。

群里的聊天记录（旧版 ``type=forward`` / 新版 ``com.tencent.multimsg``）经常带
噪声，分两类：

1. **末尾广告**：带二维码的进群海报、群推荐卡片、纯粹一条链接或「吆喝 + 链接」。
   这类东西通常就在最后几条，所以只扫末尾 ``scan_tail`` 条；
2. **装扮 / 气泡分享卡片**：``com.tencent.tuwen.lua`` + ``bizsrc=tianxuan.shareArk``，
   **任何位置都是噪声**（线上样本一条记录塞 32 张，正文一个没有，真正的内容只有
   末尾一个视频）。数量一变，同一份内容就会拼出两条指纹，所以不看位置全丢。

发送侧（``build_forward_nodes``）和入库侧（``bot.services.forward`` 的指纹清洗）
共用同一套判定，区别只在链接规则：入库可以更狠（``loose_link``），发送保持保守。

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

# 装扮 / 气泡 / 福利分享卡片：**在任何位置都是噪声**，不用看是不是末尾。
# 线上样本（群 1132313813，两条聊天记录）：一条只有 1 个视频，内层却塞了 32 张
# `com.tencent.tuwen.lua` + `bizsrc=tianxuan.shareArk` 的气泡广告做占位，
# prompt 固定是「发现超好看的装扮」，jumpUrl 指向 club.vip.qq.com 的 kuikly 装扮页。
PROMO_BIZSRC = frozenset({"tianxuan.shareark", "tianxuan.dress"})
PROMO_URL_HINTS = (
    "vas_gxh_dress_detail",
    "club.vip.qq.com/transfer",
    "tianquan.gtimg.cn/bubble",
    "tianquan.gtimg.cn/shoal/qqgxh",
)
PROMO_TEXT_HINTS = (
    "免费气泡",
    "个性装扮",
    "聊天气泡",
    "超好看的装扮",
    "气泡装扮",
)

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")
# 不带协议的裸域名 + 路径（`qm.qq.com/q/xxx`）：QQ 里的薅羊毛广告常这么写。
# 必须有路径段，免得把「我们聊 qq.com 吧」误判成链接。
BARE_URL_RE = re.compile(
    r"(?<![\w/])(?:[\w-]+\.)+(?:com|cn|net|org|xyz|top|cc|me|tv|info|link|shop|vip|site|online)"
    r"/[^\s\u4e00-\u9fff]*",
    re.IGNORECASE,
)

# 纯链接判定：把链接挖掉后剩下的字不超过这个数，就算「这条消息只是个链接」
MAX_LINK_ONLY_CHARS = 6

# 末尾几条里的「带链接小广告」：留下的话通常只有一句吆喝 + 链接
# （线上实例 `更多網摞🔥逆天⚡️发源地🌈↓ https://pd.qq.com/s/...`，去掉链接还剩 13 字）
MAX_TAIL_LINK_CHARS = 30

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
        # 装扮 / 气泡分享卡片（任意位置都丢），见 PROMO_BIZSRC
        "drop_promo": _bool(raw.get("drop_promo"), True),
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
    tail_rules = bool(
        opts.get("drop_qr") or opts.get("drop_group_card") or opts.get("drop_link")
    )
    return bool((opts.get("scan_tail") and tail_rules) or opts.get("drop_promo")) or bool(
        append.get("enable") and append.get("items")
    )


# ══════════════════════════════════════════════════════════
# 按「来源群 + 发送者」过滤自动搬运
# ══════════════════════════════════════════════════════════
def normalize_sender_filter(raw: Any) -> dict[str, frozenset[str]]:
    """把 ``auto_forward.filter`` 归一成 ``{来源群号: {发送者QQ号}}``。

    配置格式::

        [
          {"group_id": 123456, "user_id": [10001, 10002]},
          {"group_id": 234567, "user_id": [10003]}
        ]

    ``group_id`` 是**事件所在的来源群**，不是搬运目标群；``user_id`` 是这条
    聊天记录的发送者。这样可以在某些群里只屏蔽某位朋友的转发，不影响其他人。

    容错：``qgroup_id`` 旧写法、``user_id`` 单值或数组都认；空群号、空用户
    数组、格式不对的项直接忽略。配置成 ``None`` / ``False`` 等于关闭。
    """
    if raw is None or raw is False:
        return {}
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return {}

    result: dict[str, set[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        group_id = _clean_id(entry.get("group_id") or entry.get("qgroup_id"))
        if not group_id:
            continue
        users = entry.get("user_id")
        if users is None:
            users = entry.get("user_ids") or entry.get("userid_list")
        if isinstance(users, (str, int)):
            users = [users]
        if not isinstance(users, (list, tuple, set)):
            continue
        cleaned = {_clean_id(uid) for uid in users}
        cleaned.discard("")
        if not cleaned:
            continue
        result.setdefault(group_id, set()).update(cleaned)

    return {group_id: frozenset(users) for group_id, users in result.items()}


def is_sender_filtered(event: dict, raw_filter: Any) -> bool:
    """这条事件是否命中「来源群 + 发送者」过滤名单。"""
    if not isinstance(event, dict):
        return False
    filters = normalize_sender_filter(raw_filter)
    if not filters:
        return False
    group_id = _clean_id(event.get("group_id"))
    user_id = _clean_id(event.get("user_id"))
    return bool(group_id and user_id and user_id in filters.get(group_id, ()))


def _clean_id(value: Any) -> str:
    return str(value if value is not None else "").strip()


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
def _payload_is_promo(payload: dict) -> bool:
    """一张 json 卡片是不是「装扮 / 气泡 / 福利」分享（纯噪声）。"""
    if not isinstance(payload, dict):
        return False
    bizsrc = str(payload.get("bizsrc") or "").strip().lower()
    if bizsrc and (bizsrc in PROMO_BIZSRC or "shareark" in bizsrc):
        return True
    blob = [str(payload.get("prompt") or ""), str(payload.get("desc") or "")]
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for node in meta.values():
            if not isinstance(node, dict):
                continue
            for key in ("jumpUrl", "url", "preview", "title", "tag", "desc"):
                blob.append(str(node.get(key) or ""))
    joined = " ".join(part for part in blob if part)
    lowered = joined.lower()
    if any(hint in lowered for hint in PROMO_URL_HINTS):
        return True
    return any(hint in joined for hint in PROMO_TEXT_HINTS)


def promo_card_reason(msg: dict) -> str | None:
    """装扮 / 气泡分享卡片：**不看位置**，命中就是噪声。

    这类卡片是「占位」性质的：线上样本一条记录里塞了 32 张，正文一个没有，
    真正的内容只有末尾一个视频。留着它们，指纹会被卡片数量带跑（同一份内容
    换个人转发，气泡数一变就判成新的）。
    """
    for payload in iter_json_payloads(msg):
        if _payload_is_promo(payload):
            return "promo_card"
    blob = " ".join(iter_xml(msg))
    if blob:
        lowered = blob.lower()
        if any(hint in lowered for hint in PROMO_URL_HINTS):
            return "promo_card"
        if any(hint in blob for hint in PROMO_TEXT_HINTS):
            return "promo_card"
    return None


def classify_message(
    msg: dict,
    *,
    drop_qr: bool = True,
    drop_group_card: bool = True,
    drop_link: bool = True,
    loose_link: bool = False,
    image_timeout: float = DEFAULT_IMAGE_TIMEOUT,
    image_loader: Callable[[str, float], bytes | None] | None = None,
    qr_budget: list[int] | None = None,
) -> str | None:
    """判定一条（转发记录里的）消息是不是广告。

    :param loose_link: 宽松链接规则：去掉链接后还剩 ``MAX_TAIL_LINK_CHARS`` 字以内也
        算广告。只给**指纹清洗**用（末尾几条的吆喝 + 链接），发送侧保持保守，
        免得把「这个库 https://… 挺好用」这种正常聊天删掉。
    :param qr_budget: 二维码检测预算（单元素列表，会被就地扣减）。一条转发可能有
        十几张图，每张都下载解码会把入库拖到几十秒，所以整条记录共用一个额度。
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
            if qr_budget is not None:
                if qr_budget[0] <= 0:
                    break  # 额度用完就不再下载，剩下的图按「不是二维码」处理
                qr_budget[0] -= 1
            if image_has_qr(url, timeout=image_timeout, loader=image_loader):
                return "qr_image"

    if drop_link:
        body = message_text(msg).strip()
        if body:
            found = URL_RE.findall(body) or BARE_URL_RE.findall(body)
            if found:
                # 「只剩一条链接」才算广告：把链接挖掉后几乎没字了
                rest = BARE_URL_RE.sub("", URL_RE.sub("", body)).strip()
                if len(rest) <= MAX_LINK_ONLY_CHARS:
                    return "plain_link"
                if any(hint in body.lower() for hint in GROUP_URL_HINTS):
                    return "plain_link"
                if loose_link and len(rest) <= MAX_TAIL_LINK_CHARS:
                    return "plain_link"
    return None


def _iter_json_payloads_exist(msg: dict) -> bool:
    return next(iter_json_payloads(msg), None) is not None


def _is_media_first(msg: dict) -> bool:
    """第一条段不是文本（图片 / 卡片等）—— 用来把「卡片类」和纯聊天区分开。"""
    first = next(iter_segments(msg), None)
    return bool(first) and first.get("type") != "text"


def filter_context(msg:dict,context:list) -> bool:
    content = message_text(msg=msg)
    for sub_context in context:
        if re.search(sub_context,content):
            return True
    return False


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


def download_image_ex(
    url: str,
    timeout: float = DEFAULT_IMAGE_TIMEOUT,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes | None, str]:
    """下载图片字节，失败时给出**人能看懂的原因**。

    :param max_bytes: 自定义大小上限；缺省用 ``MAX_IMAGE_BYTES``（8MB）。
        **不要用「只下载头部」的截断图做判定**——实测 GIF 截断解码出的画面
        和原图内容完全对不上（色图 GIF 部分图打分 ~0.001，原图 0.97+），
        会把色图漏成安全图；要判大图就全量下载（调大 max_bytes）。
    :return: ``(字节流或 None, 失败原因)``；成功时原因为 ``""``。
    原因示例：``超过大小上限(25MB>8MB)`` / ``HTTP 404（多半是链接里的 rkey 过期）`` /
    ``下载超时(8s)`` / ``请求异常: xxx`` / ``不是HTTP链接``。
    """
    if requests is None or not url.startswith(("http://", "https://")):
        return None, "不是HTTP链接"
    limit = max_bytes if (max_bytes and max_bytes > 0) else MAX_IMAGE_BYTES
    last_error = ""
    for proxies in (None, {"http": None, "https": None}):
        try:
            resp = requests.get(url, timeout=timeout, proxies=proxies, stream=True)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}（多半是链接里的 rkey 过期）"
                continue
            length = int(resp.headers.get("Content-Length") or 0)
            if length and length > limit:
                return None, f"超过大小上限({length // 1024 // 1024}MB>{limit // 1024 // 1024}MB)"
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > limit:
                    return None, f"超过大小上限(>={limit // 1024 // 1024}MB)"
                chunks.append(chunk)
            return b"".join(chunks), ""
        except requests.exceptions.Timeout:
            last_error = f"下载超时({timeout:g}s)"
        except Exception as exc:
            last_error = f"请求异常: {exc.__class__.__name__}"
    return None, last_error or "未知原因"


def download_image(url: str, timeout: float = DEFAULT_IMAGE_TIMEOUT) -> bytes | None:
    """下载图片字节；超过 ``MAX_IMAGE_BYTES`` 或失败返回 None。

    先按环境（含系统代理）下载，失败再强制直连 —— 内网代理挂掉时 QQ 的
    图片域名往往仍然直连可达。要看失败原因用 :func:`download_image_ex`。
    """
    return download_image_ex(url, timeout)[0]


# ══════════════════════════════════════════════════════════
# 裁剪 + 重建
# ══════════════════════════════════════════════════════════
def clean_forward_messages(
    messages: list[dict],
    *,
    scan_tail: int = DEFAULT_SCAN_TAIL,
    drop_promo: bool = True,
    drop_qr: bool = True,
    drop_group_card: bool = True,
    drop_link: bool = True,
    loose_link: bool = False,
    image_timeout: float = DEFAULT_IMAGE_TIMEOUT,
    image_loader: Callable[[str, float], bytes | None] | None = None,
    max_qr_images: int = 3,
) -> tuple[list[dict], list[dict]]:
    """把转发记录里的噪声整条丢掉，分两遍：

    1. **装扮 / 气泡分享卡片**（``drop_promo``）：不看位置。这类卡片是占位噪声，
       数量还会变，留着会让指纹跟着卡片数漂移；
    2. **末尾广告**（``scan_tail`` + qr / group_card / link）：只扫最后几条，
       二维码海报、群推荐卡片、纯链接（``loose_link`` 时放宽到「吆喝 + 链接」）。

    :return: ``(保留的消息, 丢弃记录)``；丢弃记录形如
        ``{"index": 31, "reason": "qr_image", "preview": ""}``（按原下标升序）
    """
    kept: list[dict] = []
    removed: list[dict] = []
    survivors: list[tuple[int, dict]] = []
    for index, msg in enumerate(messages or []):
        reason = promo_card_reason(msg) if drop_promo else None
        if reason:
            removed.append({"index": index, "reason": reason, "preview": preview_of(msg)})
        else:
            survivors.append((index, msg))

    tail_from = (
        max(0, len(survivors) - max(0, scan_tail)) if scan_tail else len(survivors)
    )
    qr_budget = [max(0, int(max_qr_images))]
    for position, (index, msg) in enumerate(survivors):
        if position < tail_from:
            kept.append(msg)
            continue
        reason = classify_message(
            msg,
            drop_qr=drop_qr,
            drop_group_card=drop_group_card,
            drop_link=drop_link,
            loose_link=loose_link,
            image_timeout=image_timeout,
            image_loader=image_loader,
            qr_budget=qr_budget,
        )
        if reason:
            removed.append({"index": index, "reason": reason, "preview": preview_of(msg)})
        else:
            kept.append(msg)
    removed.sort(key=lambda item: item["index"])
    return kept, removed


def strip_tail_ads(messages: list[dict], **kwargs) -> tuple[list[dict], list[dict]]:
    """旧名字：等价于 :func:`clean_forward_messages`（发送侧的裁剪入口）。"""
    return clean_forward_messages(messages, **kwargs)


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
    tail_rules = bool(
        opts.get("drop_qr") or opts.get("drop_group_card") or opts.get("drop_link")
    )
    if opts.get("drop_promo") or (opts.get("scan_tail") and tail_rules):
        kept, removed = clean_forward_messages(
            messages,
            scan_tail=opts.get("scan_tail", DEFAULT_SCAN_TAIL),
            drop_promo=bool(opts.get("drop_promo", True)),
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
    "BARE_URL_RE",
    "DEFAULT_SCAN_TAIL",
    "GROUP_TEXT_HINTS",
    "GROUP_URL_HINTS",
    "QR_AVAILABLE",
    "PROMO_BIZSRC",
    "PROMO_TEXT_HINTS",
    "PROMO_URL_HINTS",
    "build_append_nodes",
    "build_forward_nodes",
    "build_nodes",
    "card_urls_and_text",
    "classify_message",
    "clean_forward_messages",
    "content_to_segments",
    "download_image",
    "image_has_qr",
    "inline_local_image",
    "is_enabled",
    "is_sender_filtered",
    "message_text",
    "normalize_sender_filter",
    "normalize_settings",
    "preview_of",
    "promo_card_reason",
    "qr_in_bytes",
    "strip_tail_ads",
    "filter_context",
]
