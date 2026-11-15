"""收藏表情（账号自定义表情）的跨后端抽象。

这一层解决两件事：

1. **两个后端给的数据字段不一样**，得先归一成同一种形状；
2. **两个后端的发送通道不一样**，得先决定走哪条路。

实测（2026-11-01，NapCat:3000 / SnowLuma:3210，同一个测试群 1132313813）：

``fetch_custom_face_detail`` 的返回
=================================  ==================================  ==================================
字段                                SnowLuma                           NapCat
=================================  ==================================  ==================================
``emoji_id``                        ✅（等于 resId）                  ❌ **没有这个字段**
``resId``                           ✅                                ✅
``url`` / ``md5``                   ✅                                ✅
``desc``                            ✅（用户自己写的备注）            ✅
``uin`` / ``emoId``                 ❌                                ✅（账号内序号）
``emoPath`` / ``thumbPath``         ❌                                ✅（本地路径，可能不存在）
=================================  ==================================  ==================================

所以「表情标识」在 SnowLuma 叫 ``emoji_id``、在 NapCat 叫 ``resId``，
字段名直接写死必然踩坑——能力表里的 :attr:`BackendCapabilities.custom_face_id_field`
就是干这个的。

**主键用 ``desc``（用户自己写的备注）**，理由是它跨账号稳定：

- 同一个表情在两个账号上，``md5`` 与 ``desc`` 完全一致，
  但 ``emoji_id`` / ``resId`` 带的是**账号自己的 uin 前缀**
  （``3122793026_0_0_0_<MD5>_0_0`` vs ``3521775163_0_0_0_<MD5>_0_0``），
  换账号就对不上了；
- ``desc`` 是人话，模型能看懂、能直接写进 ``<meme>名字</meme>``；
- ``desc`` 可能为空（用户没起名），这时退回 ``md5``——同样是内容哈希，
  一样跨账号稳定。

发送
====

- **SnowLuma** 有专用动作 ``send_custom_face(emoji_id, group_id|user_id, reply_to?)``，
  ``emoji_id`` 可以传完整 ID，也可以传 32 位 MD5。
- **NapCat 没有**这个动作（实测返回 ``retcode=200「不支持的Api send_custom_face」``）。
  但用同一个 image 段发，收藏表情的外观能保住::

      {"type": "image", "data": {"file": <表情URL>, "sub_type": 1, "summary": "[动画表情]"}}

  少了 ``sub_type`` 就会退化成普通图片——同一个表情 URL，回读是
  ``sub_type=0 / summary="[图片]"``；带上就是 ``sub_type=1 / summary="[动画表情]"``。

  兜底段在两个后端上都验证过（SnowLuma 的 ``send_custom_face`` 内部发的
  也就是它），所以「没有专用动作就发 image 段」是安全的降级。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .kernal import ApiResult, OneBotKernel

__all__ = [
    "CustomFace",
    "CustomFaceLibrary",
    "CustomFaceService",
    "fetch_custom_faces",
    "normalize_face_key",
]


#: 表情标识字段的候选名：不同后端叫法不同，按顺序挨个试。
_ID_FIELDS = ("emoji_id", "resId", "res_id", "id")

#: key 归一化时剥掉的装饰词（跟表情包库那套保持一致）
_NOISE_WORDS = ("表情包", "表情", "贴纸", "图片")

#: 归一化时要去掉的标点 / 空白
_PUNCT = re.compile(r"[\s\[\]【】()（）「」『』:：,，。.!！?？~～]+")


def normalize_face_key(text: Any) -> str:
    """把表情名归一化成可比较的 key。

    去空白 / 方括号 / 标点，去掉「表情」这类装饰词，再转小写。
    模型爱写「不够色情表情」「【不够色情】」这种，不能因此判成不存在。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return ""
    raw = _PUNCT.sub("", raw)
    for word in _NOISE_WORDS:
        raw = raw.replace(word, "")
    return raw.lower()


@dataclass(frozen=True)
class CustomFace:
    """一个收藏表情。

    :param key: 稳定主键。优先用 ``desc``（用户备注），为空时退回 ``md5``。
    :param desc: 用户自己写的备注，可能为空。
    :param md5: 表情内容哈希（32 位大写 hex），跨账号稳定。
    :param emoji_id: **账号内**的资源标识（SnowLuma 是 ``emoji_id``，
        NapCat 是 ``resId``），换账号就变，只能在当前账号上用。
    :param url: 表情图片地址（``p.qpic.cn/qq_expression/...``）。
    """

    key: str
    desc: str = ""
    md5: str = ""
    emoji_id: str = ""
    url: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """给人看的名字：有备注用备注，没有就用 md5。"""
        return self.desc or self.md5 or self.key

    def segment(
        self,
        *,
        sub_type: int = 1,
        summary: str = "[动画表情]",
    ) -> dict:
        """转成 OneBot 的 image 段（收藏表情）。

        ``sub_type=1`` 是关键：它让 QQ 把它当收藏表情而不是普通图片。
        """
        return {
            "type": "image",
            "data": {
                "file": self.url,
                "sub_type": sub_type,
                "summary": summary,
            },
        }


class CustomFaceLibrary:
    """某个账号的收藏表情清单。构造后不可变，线程安全。"""

    def __init__(self, faces: Iterable[CustomFace] = ()) -> None:
        self.faces: tuple[CustomFace, ...] = tuple(faces)
        self._by_key: dict[str, CustomFace] = {}
        for face in self.faces:
            key = normalize_face_key(face.key)
            if key:
                # 同名取先出现的：后端的收藏顺序是用户自己排的，先排的更常用
                self._by_key.setdefault(key, face)

    # ── 容器协议 ──
    def __len__(self) -> int:
        return len(self.faces)

    def __bool__(self) -> bool:
        return bool(self.faces)

    def __iter__(self) -> Iterator[CustomFace]:
        return iter(self.faces)

    def __getitem__(self, index: int) -> CustomFace:
        return self.faces[index]

    # ── 查询 ──
    def get(self, key: Any) -> CustomFace | None:
        """按 key 精确查（key 会被归一化）。"""
        normalized = normalize_face_key(key)
        return self._by_key.get(normalized) if normalized else None

    def find(self, query: Any) -> CustomFace | None:
        """按名字宽松查：先精确，再「名字互相包含」。

        模型写「不够色情表情」也能命中「不够色情」。
        """
        normalized = normalize_face_key(query)
        if not normalized:
            return None

        exact = self._by_key.get(normalized)
        if exact is not None:
            return exact

        candidates = [
            face
            for face in self.faces
            if len(normalize_face_key(face.key)) >= 2
            and (
                normalize_face_key(face.key) in normalized
                or normalized in normalize_face_key(face.key)
            )
        ]
        if candidates:
            candidates.sort(key=lambda f: abs(len(normalize_face_key(f.key)) - len(normalized)))
            return candidates[0]
        return None

    def search(self, keyword: Any, limit: int = 5) -> list[CustomFace]:
        """按关键词搜（匹配备注或 md5）。"""
        normalized = normalize_face_key(keyword)
        if not normalized:
            return []
        hits = [
            face
            for face in self.faces
            if normalized in normalize_face_key(face.desc)
            or normalized in normalize_face_key(face.md5)
        ]
        return hits[: max(1, limit)]

    def catalog(self, limit: int = 0) -> list[str]:
        """给提示词用的目录行：``名字：描述``。"""
        faces = self.faces if limit <= 0 else self.faces[:limit]
        return [f"{face.label}：收藏表情" for face in faces]


def _pick_id(item: dict, preferred: str) -> str:
    """从一条详情里取出「账号内表情标识」。

    优先用能力表指名的字段（SnowLuma ``emoji_id`` / NapCat ``resId``），
    拿不到就按候选名挨个试——后端字段会变，别写死。
    """
    for name in (preferred, *_ID_FIELDS):
        value = item.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _parse_faces(
    data: Any,
    *,
    id_field: str,
) -> tuple[CustomFace, ...]:
    """把 ``fetch_custom_face_detail`` 的返回归一成 :class:`CustomFace` 列表。"""
    if not isinstance(data, list):
        return ()

    faces: list[CustomFace] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        desc = str(item.get("desc") or "").strip()
        md5 = str(item.get("md5") or "").strip()
        emoji_id = _pick_id(item, id_field)
        url = str(item.get("url") or "").strip()

        # 主键：备注优先，没有备注就用内容哈希（两者都跨账号稳定）
        key = desc or md5
        if not key:
            continue

        faces.append(
            CustomFace(
                key=key,
                desc=desc,
                md5=md5,
                emoji_id=emoji_id,
                url=url,
                raw=dict(item),
            )
        )
    return tuple(faces)


async def fetch_custom_faces(
    kernel: OneBotKernel,
    *,
    count: int | None = None,
) -> tuple[CustomFace, ...]:
    """抓当前账号的收藏表情。

    只用 ``fetch_custom_face_detail``——它带 ``desc``（也就是我们要的主键）。
    ``fetch_custom_face`` 只给 URL 列表，没有备注，当主键用不了。
    """
    caps = kernel.capabilities
    limit = int(count or caps.custom_face_max_count)

    result = await kernel.call("fetch_custom_face_detail", {"count": limit})
    if not result.ok:
        return ()
    return _parse_faces(result.data, id_field=caps.custom_face_id_field)


class CustomFaceService:
    """把「按 key 发表情」这件事封起来。

    典型用法::

        service = CustomFaceService(kernel)
        await service.refresh()
        await service.send("不够色情", group_id=20001)
    """

    def __init__(
        self,
        kernel: OneBotKernel,
        *,
        library: CustomFaceLibrary | None = None,
    ) -> None:
        self.kernel = kernel
        self._library = library or CustomFaceLibrary()

    @property
    def library(self) -> CustomFaceLibrary:
        return self._library

    async def refresh(self, *, count: int | None = None) -> CustomFaceLibrary:
        """重新抓一遍收藏表情，替换本地清单。"""
        faces = await fetch_custom_faces(self.kernel, count=count)
        self._library = CustomFaceLibrary(faces)
        return self._library

    def get(self, key: Any) -> CustomFace | None:
        """按 key 查（精确 → 宽松）。"""
        return self._library.find(key)

    async def send(
        self,
        key: Any,
        *,
        group_id: Any = None,
        user_id: Any = None,
        reply_to: Any = None,
    ) -> ApiResult:
        """按 key 发送收藏表情。

        后端有专用动作（SnowLuma 的 ``send_custom_face``）就走专用动作，
        否则退回 image 段——两条路都能保住「收藏表情」的外观。

        找不到 key 时返回 ``ok=False`` 的结果，不抛异常：
        发错表情比不发更尴尬，让调用方决定要不要降级。
        """
        if group_id is None and user_id is None:
            return ApiResult(
                ok=False,
                action="send_custom_face",
                error="group_id 与 user_id 必须给一个",
            )

        face = self.get(key)
        if face is None:
            return ApiResult(
                ok=False,
                action="send_custom_face",
                error=f"收藏表情里没有 {key!r}",
            )

        caps = self.kernel.capabilities
        action = caps.custom_face_send_action
        if action:
            params: dict[str, Any] = {
                caps.custom_face_send_id_param: face.emoji_id or face.md5,
            }
            if group_id is not None:
                params["group_id"] = group_id
            else:
                params["user_id"] = user_id
            if reply_to is not None:
                params["reply_to"] = reply_to
            return await self._safe_call(action, params)

        # 兜底：image 段。SnowLuma 的 send_custom_face 内部发的也是它。
        message: list[dict] = []
        if reply_to is not None:
            message.append({"type": "reply", "data": {"id": str(reply_to)}})
        message.append(
            face.segment(
                sub_type=caps.custom_face_sub_type,
                summary=caps.custom_face_summary,
            )
        )

        if group_id is not None:
            return await self._safe_call(
                "send_group_msg", {"group_id": group_id, "message": message}
            )
        return await self._safe_call(
            "send_private_msg", {"user_id": user_id, "message": message}
        )

    async def _safe_call(self, action: str, params: dict) -> ApiResult:
        """发包并把异常也收敛成 ``ok=False`` 的结果。

        发消息这类动作在能力表的 ``strict_actions`` 里，失败时底层会直接抛
        :class:`OneBotAPIError`。这里统一收敛成失败结果，调用方只需要判断
        ``ok``，不用既判异常又判返回值。
        """
        try:
            return await self.kernel.call(action, params)
        except Exception as exc:  # noqa: BLE001 - 统一收敛成失败结果
            return ApiResult(ok=False, action=action, error=f"{type(exc).__name__}: {exc}")
