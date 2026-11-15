"""表情包库：按名字找表情，渲染成 NapCat 消息段 / CQ 码。

数据格式跟 Meme.json 一致，分四类：

- ``faces``：QQ 内置小表情，只有数字 id，走 ``[CQ:face,id=…]``；
- ``mfaces``：QQ 商城表情包（就是群里那种大表情），需要
  ``emoji_package_id`` + ``emoji_id`` + ``key``，走 ``mface`` 消息段；
- ``stickers``：本地图片，走 ``[CQ:image,file=…]``；
- ``customs``：**账号收藏表情**（QQ 收藏夹里那些），运行时从后端抓，
  走 ``image`` 段 + ``sub_type=1``，保留收藏表情的外观。

模型只会写名字（``<meme>嘬嘬</meme>``），而名字是从提示词里的目录抄的，
很容易多写一个「表情」、少写一个字。所以匹配按三级退让：

    完全一致 → 名字互相包含 → 描述里命中

三级都没命中就返回 None —— 宁可这一轮不发，也不要瞎发一个不相关的表情
（发错表情比不发更尴尬）。

名字与数据的对应关系全部来自配置文件，改表情包不用改代码。

收藏表情是唯一「运行时才有」的一类：它不在 JSON 里，而是启动时从后端
``fetch_custom_face_detail`` 抓回来，用**用户自己写的备注（desc）**当名字。
两个后端给的字段名不一样（SnowLuma 是 ``emoji_id``，NapCat 是 ``resId``），
归一化在 :mod:`AbstractionLayer.face` 里做，这里只管渲染。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("JungJing.Messaging.Meme")

# mface 的 summary 是「[嘬嘬]」这种带方括号的字符串，直接拼进 CQ 码会被
# 当成 CQ 码的结束括号，所以按 OneBot 约定转义。
_CQ_ESCAPES = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"), (",", "&#44;"))
_CQ_UNESCAPES = (
    ("&#44;", ","),
    ("&#93;", "]"),
    ("&#91;", "["),
    ("&amp;", "&"),
)

# 名字里常见的装饰词，匹配前先去掉
_NOISE_WORDS = ("表情包", "表情", "贴纸", "图片", "的", "那个", "一个")


def cq_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    for raw, escaped in _CQ_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def cq_unescape(value: Any) -> str:
    text = str(value if value is not None else "")
    for escaped, raw in _CQ_UNESCAPES:
        text = text.replace(escaped, raw)
    return text


def normalize_name(text: Any) -> str:
    """归一化名字：去空白 / 方括号 / 标点，去掉「表情」这类装饰词。"""
    raw = str(text if text is not None else "").strip()
    if not raw:
        return ""
    raw = re.sub(r"[\s\[\]【】()（）「」『』:：,，。.!！?？~～]+", "", raw)
    for word in _NOISE_WORDS:
        raw = raw.replace(word, "")
    return raw.lower()


#: 收藏表情在 image 段里的 ``sub_type``：1 才是收藏表情，0 会退化成普通图片。
CUSTOM_FACE_SUB_TYPE = 1

#: 收藏表情的显示摘要（QQ 客户端里收藏表情就叫「动画表情」）。
CUSTOM_FACE_SUMMARY = "[动画表情]"


@dataclass(frozen=True)
class MemeEntry:
    """一条表情：名字、描述、类型和发送所需的数据。"""

    name: str
    describe: str = ""
    kind: str = "mface"          # face / mface / sticker / custom
    data: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    def segment(self) -> dict:
        """转成 NapCat 消息段。"""
        data = {k: v for k, v in self.data.items() if v not in (None, "")}
        if self.kind == "face":
            return {"type": "face", "data": {"id": str(data.get("id") or "")}}
        if self.kind == "sticker":
            return {
                "type": "image",
                "data": {"file": str(data.get("file") or data.get("path") or "")},
            }
        if self.kind == "custom":
            # 收藏表情：靠 sub_type=1 保住「收藏表情」的外观，
            # 少了它 QQ 会当成普通图片发出去。
            return {
                "type": "image",
                "data": {
                    "file": str(data.get("url") or data.get("file") or ""),
                    "sub_type": int(data.get("sub_type") or CUSTOM_FACE_SUB_TYPE),
                    "summary": str(data.get("summary") or CUSTOM_FACE_SUMMARY),
                },
            }
        return {"type": "mface", "data": data}

    def cq(self) -> str:
        """转成 CQ 码（可以混在普通文本里一起发）。"""
        if self.kind == "face":
            return f"[CQ:face,id={cq_escape(self.data.get('id', ''))}]"
        if self.kind == "sticker":
            file = self.data.get("file") or self.data.get("path") or ""
            return f"[CQ:image,file={cq_escape(file)}]"
        if self.kind == "custom":
            file = self.data.get("url") or self.data.get("file") or ""
            sub_type = int(self.data.get("sub_type") or CUSTOM_FACE_SUB_TYPE)
            summary = str(self.data.get("summary") or CUSTOM_FACE_SUMMARY)
            # sub_type 必须一起带上：CQ 码解析会把没写进 data 的字段丢掉，
            # 少了它收藏表情就退化成普通图片了。
            return (
                f"[CQ:image,file={cq_escape(file)},"
                f"sub_type={sub_type},summary={cq_escape(summary)}]"
            )
        parts = []
        for key in ("emoji_id", "emoji_package_id", "key", "summary"):
            value = self.data.get(key)
            if value in (None, ""):
                continue
            parts.append(f"{key}={cq_escape(value)}")
        return f"[CQ:mface,{','.join(parts)}]" if parts else ""

    def label(self) -> str:
        return f"<{self.name}>"


def mface_cq(data: dict) -> str:
    """商城表情的数据 → CQ 码。

    独立于 :class:`MemeEntry`：有些场景要发一个**固定**的表情（比如搬运去重时
    回一句「这条已经发过了」），它不在表情库里，也没有「名字」可填，只有
    ``emoji_id`` / ``emoji_package_id`` / ``key`` / ``summary`` 四件套。

    ``summary`` 带方括号，直接拼进 CQ 码会被当成 CQ 码的结束括号，所以走
    :func:`cq_escape` 转义（和表情库里那 81 个商城表情同一条路）。
    """
    entry = MemeEntry(name="mface", kind="mface", data=data or {})
    return entry.cq()


class MemeLibrary:
    """表情集合。构造后不可变，线程安全。"""

    def __init__(self, entries: Iterable[MemeEntry] = ()):
        self.entries: tuple[MemeEntry, ...] = tuple(
            entry for entry in entries if entry.name and entry.data
        )
        self._by_key: dict[str, MemeEntry] = {}
        for entry in self.entries:
            self._by_key.setdefault(entry.key, entry)

    # ── 构造 ──
    @classmethod
    def from_dict(cls, data: dict) -> "MemeLibrary":
        entries: list[MemeEntry] = []
        for kind in ("faces", "mfaces", "stickers", "customs"):
            payload = (data or {}).get(kind)
            if not isinstance(payload, list):
                continue
            single = kind[:-1] if kind.endswith("s") else kind   # faces -> face
            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                body = item.get("data")
                if not name or not isinstance(body, dict):
                    continue
                entries.append(
                    MemeEntry(
                        name=name,
                        describe=str(item.get("describe") or "").strip(),
                        kind=single,
                        data=body,
                    )
                )
        return cls(entries)

    def merged_with_custom_faces(self, faces: Iterable[Any]) -> "MemeLibrary":
        """返回一个并入「账号收藏表情」的新库。

        收藏表情是运行时从后端抓的，名字用用户自己写的备注（desc），
        没有备注就用 md5。同名时**静态配置优先**——配置文件是人工维护的，
        收藏表情只是补充，不该把配好的表情顶掉。

        ``faces`` 接受 :class:`AbstractionLayer.face.CustomFace`，
        也接受长得一样的 dict（方便测试与落盘）。
        """
        from AbstractionLayer.face import CustomFace

        extra: list[MemeEntry] = []
        for face in faces or ():
            if isinstance(face, CustomFace):
                name = face.label
                describe = "收藏表情"
                if not face.url:
                    continue      # 没有 URL 渲染不出来（发出去会是个空图片）
                body = {
                    "url": face.url,
                    "emoji_id": face.emoji_id,
                    "md5": face.md5,
                    "sub_type": CUSTOM_FACE_SUB_TYPE,
                    "summary": CUSTOM_FACE_SUMMARY,
                }
            elif isinstance(face, dict):
                name = str(face.get("name") or face.get("desc") or "").strip()
                describe = str(face.get("describe") or "收藏表情")
                body = dict(face.get("data") or {})
                if not (body.get("url") or body.get("file")):
                    continue
                body.setdefault("sub_type", CUSTOM_FACE_SUB_TYPE)
                body.setdefault("summary", CUSTOM_FACE_SUMMARY)
            else:
                continue
            if not name:
                continue
            extra.append(
                MemeEntry(name=name, describe=describe, kind="custom", data=body)
            )

        known = {entry.key for entry in self.entries}
        merged = list(self.entries)
        for entry in extra:
            if not entry.key:
                continue          # 名字归一化后是空的，等于没法按名字找
            if entry.key in known:
                continue          # 静态配置优先
            known.add(entry.key)
            merged.append(entry)
        return MemeLibrary(merged)

    @classmethod
    def load(cls, path: str | Path) -> "MemeLibrary":
        file = Path(path)
        if not file.exists():
            logger.warning("[表情] 表情包配置不存在：%s", file)
            return cls()
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("[表情] 读取 %s 失败：%s", file, exc)
            return cls()
        return cls.from_dict(data if isinstance(data, dict) else {})

    # ── 查询 ──
    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, name: Any) -> Optional[MemeEntry]:
        key = normalize_name(name)
        return self._by_key.get(key) if key else None

    def find(self, query: Any) -> Optional[MemeEntry]:
        """按名字或描述找一条；找不到返回 None。"""
        raw = str(query if query is not None else "").strip()
        key = normalize_name(raw)
        if not key:
            return None

        exact = self._by_key.get(key)
        if exact is not None:
            return exact

        # 名字互相包含：「嘬」→「嘬嘬」，「嘬嘬表情」→「嘬嘬」
        candidates = [
            entry
            for entry in self.entries
            if len(entry.key) >= 2 and (entry.key in key or key in entry.key)
        ]
        if candidates:
            # 差得越少越可能是同一个
            candidates.sort(key=lambda e: abs(len(e.key) - len(key)))
            return candidates[0]

        # 描述命中：模型可能写「开心」而不是「嘬嘬」
        described = [entry for entry in self.entries if key in normalize_name(entry.describe)]
        if described:
            described.sort(key=lambda e: len(e.describe))
            return described[0]
        return None

    def search(self, keyword: Any, limit: int = 5) -> list[MemeEntry]:
        key = normalize_name(keyword)
        if not key:
            return []
        hits = [
            entry
            for entry in self.entries
            if key in entry.key or key in normalize_name(entry.describe)
        ]
        return hits[: max(1, limit)]

    def random(self) -> Optional[MemeEntry]:
        import random

        return random.choice(self.entries) if self.entries else None

    def catalog(self, limit: int = 0) -> list[str]:
        """给提示词用的目录行：``名字：描述``。limit<=0 表示不限。"""
        entries = self.entries if limit <= 0 else self.entries[:limit]
        lines: list[str] = []
        for entry in entries:
            desc = entry.describe or entry.name
            lines.append(f"{entry.name}：{desc}")
        return lines

    # ── 渲染 ──
    def cq(self, query: Any) -> str:
        entry = self.find(query)
        return entry.cq() if entry else ""


# ════════════════════ CQ 码 → 消息段 ════════════════════
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z_]+)((?:,[^\[\]]*)?)\]")

# 这些段在 CQ 码里字段名跟消息段字段名不一致，需要显式映射
_CQ_FIELDS = {
    "at": (("qq", "qq"),),
    "reply": (("id", "id"),),
    "image": (
        ("file", "file"),
        ("url", "url"),
        ("summary", "summary"),
        ("sub_type", "sub_type"),
    ),
    "face": (("id", "id"),),
    "mface": (
        ("emoji_id", "emoji_id"),
        ("emoji_package_id", "emoji_package_id"),
        ("key", "key"),
        ("summary", "summary"),
    ),
}


def has_mface(text: Any) -> bool:
    return "[CQ:mface" in str(text if text is not None else "")


def has_cq_segment(text: Any) -> bool:
    """这条文本里有没有需要转成消息段的 CQ 码。

    两类要转：

    - ``[CQ:mface,…]``：商城表情，``summary`` 带方括号，交给 CQ 解析容易被截断；
    - 带 ``sub_type`` 的 ``[CQ:image,…]``：**收藏表情**。少了 ``sub_type``
      收藏表情会退化成普通图片，所以这一类也必须转成段数组原样送出去。
    """
    raw = str(text if text is not None else "")
    if "[CQ:mface" in raw:
        return True
    return "[CQ:image" in raw and "sub_type=" in raw


def is_custom_face_cq(text: Any) -> bool:
    """这条文本里有没有收藏表情（带 ``sub_type`` 的 image CQ 码）。"""
    raw = str(text if text is not None else "")
    return "[CQ:image" in raw and "sub_type=" in raw


def _parse_cq_params(raw: str) -> dict:
    params: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        if key:
            params[key] = cq_unescape(value)
    return params


def _mface_data(params: dict) -> dict:
    """mface 段的数据。``emoji_package_id`` 在 NapCat 里是数值，别塞成字符串。"""
    data: dict[str, Any] = {}
    for key in ("emoji_id", "key", "summary"):
        if params.get(key):
            data[key] = params[key]
    package = params.get("emoji_package_id")
    if package not in (None, ""):
        try:
            data["emoji_package_id"] = int(package)
        except (TypeError, ValueError):
            data["emoji_package_id"] = package
    return data


def _image_data(params: dict) -> dict:
    """image 段的数据。

    ``sub_type`` 还原成数字：它是「收藏表情」的开关，字符串 ``"1"``
    在 NapCat 里会被原样塞进 ``picSubType``，不保险。
    """
    data: dict[str, Any] = {}
    for key in ("file", "url", "summary"):
        if params.get(key):
            data[key] = params[key]
    sub_type = params.get("sub_type")
    if sub_type not in (None, ""):
        try:
            data["sub_type"] = int(sub_type)
        except (TypeError, ValueError):
            data["sub_type"] = sub_type
    return data


def to_segments(text: Any) -> Optional[list[dict]]:
    """把含表情包的 CQ 码字符串切成消息段数组；不含则返回 None。

    为什么要转：mface 的 ``summary`` 带方括号，作为 CQ 码交给 NapCat 解析时
    容易被截断；直接发消息段数组不经过 CQ 码解析，最稳。收藏表情靠
    ``sub_type`` 保留收藏表情外观，同一个理由。其余 CQ 码顺手一起
    转过去，保证「文字 + 表情回应 + 表情包」混排时的顺序不乱。

    返回 None 表示「这条没有表情包」——调用方继续按原来的字符串发送，
    旧路径完全不受影响。
    """
    raw = str(text if text is not None else "")
    if not has_cq_segment(raw):
        return None

    segments: list[dict] = []

    def add_text(value: str) -> None:
        if not value:
            return
        if segments and segments[-1].get("type") == "text":
            segments[-1]["data"]["text"] += value
        else:
            segments.append({"type": "text", "data": {"text": value}})

    position = 0
    for matched in _CQ_PATTERN.finditer(raw):
        add_text(raw[position : matched.start()])
        cq_type = matched.group(1).lower()
        params = _parse_cq_params(matched.group(2))
        fields = _CQ_FIELDS.get(cq_type)
        if fields is None:
            data = dict(params)
        elif cq_type == "mface":
            data = _mface_data(params)
        elif cq_type == "image":
            data = _image_data(params)
        else:
            data = {target: params[source] for source, target in fields if params.get(source)}
        if cq_type == "at" and not data.get("qq"):
            data["qq"] = "all"
        segments.append({"type": cq_type, "data": data})
        position = matched.end()
    add_text(raw[position:])

    # 只有空白文本的消息等于没内容
    if not any(
        seg["type"] != "text" or seg["data"].get("text", "").strip() for seg in segments
    ):
        return None
    return segments or None


# ════════════════════ 单例 ════════════════════
_default_library: Optional[MemeLibrary] = None
_default_path: str = ""
_default_enabled: bool = True
#: 运行时从后端抓来的收藏表情。单独存一份，configure() 重载配置时要再并回去——
#: 否则改一次配置，刚抓到的收藏表情就全没了。
_default_custom_faces: list = []


def default_path() -> Path:
    """默认表情包文件：``data/memes.json``。"""
    try:
        from config.paths import AppPaths

        return AppPaths.discover().data_file("memes.json")
    except Exception:  # noqa: BLE001 - 路径算不出来也不能炸
        return Path(__file__).resolve().parents[1] / "data" / "memes.json"


def _resolve_path(path: str | Path | None) -> Path:
    """相对路径：带目录的按项目根解析，只有文件名的按 ``data/`` 解析。"""
    if path is None or str(path).strip() == "":
        return default_path()
    target = Path(str(path).strip()).expanduser()
    if target.is_absolute():
        return target
    root = default_path().parent.parent       # <项目根>/data -> <项目根>
    if target.parent == Path("."):
        return root / "data" / target.name
    return root / target


def _base_library() -> MemeLibrary:
    """配置文件里那一份「干净」的库（不含运行时抓的收藏表情）。"""
    if not _default_enabled:
        return MemeLibrary()
    path = _default_path or str(default_path())
    return MemeLibrary.load(path)


def configure(path: str | Path | None = None, *, enable: bool = True) -> MemeLibrary:
    """配置并重载默认库（启动 / 面板改配置后调用）。

    收藏表情**不会**因为重载而丢失：它们是运行时抓的，存在
    ``_default_custom_faces`` 里，这里重新并回去。Agent 热重载会走
    ``configure()``，不并回去的话表情目录就被清空了。
    """
    global _default_library, _default_path, _default_enabled
    target = _resolve_path(path)
    _default_path = str(target)
    _default_enabled = bool(enable)
    base = MemeLibrary.load(target) if enable else MemeLibrary()
    _default_library = base.merged_with_custom_faces(_default_custom_faces)
    if enable:
        logger.info("[表情] 已加载 %d 个表情（%s）", len(_default_library), target)
    return _default_library


def get_library() -> MemeLibrary:
    if _default_library is None:
        configure()
    return _default_library or MemeLibrary()


def merge_custom_faces(faces: Iterable[Any]) -> MemeLibrary:
    """把后端抓到的**账号收藏表情**并进默认库，返回合并后的库。

    收藏表情是运行时才有的（不在 JSON 里），启动时抓一次并进来，
    之后模型就能像用静态表情包一样写 ``<meme>备注名</meme>``。

    是「替换」不是「追加」：同一个 key（备注名）只保留最新抓到的那份，
    换账号 / 重抓都不会留下旧数据。库还没配置时先配置一次：否则合并结果
    会被下一次 ``configure()`` 冲掉。
    """
    global _default_custom_faces, _default_library
    items = list(faces or ())
    _default_custom_faces = items
    _default_library = _base_library().merged_with_custom_faces(items)
    logger.info(
        "[表情] 已并入 %d 个收藏表情，当前共 %d 个",
        len(items),
        len(_default_library),
    )
    return _default_library


def is_enabled() -> bool:
    if _default_library is None:
        configure()
    return _default_enabled


def reset() -> None:
    """测试用：清掉单例。"""
    global _default_library, _default_path, _default_enabled, _default_custom_faces
    _default_library = None
    _default_path = ""
    _default_enabled = True
    _default_custom_faces = []


__all__ = [
    "CUSTOM_FACE_SUB_TYPE",
    "CUSTOM_FACE_SUMMARY",
    "MemeEntry",
    "MemeLibrary",
    "configure",
    "cq_escape",
    "cq_unescape",
    "default_path",
    "get_library",
    "has_cq_segment",
    "has_mface",
    "is_custom_face_cq",
    "is_enabled",
    "mface_cq",
    "merge_custom_faces",
    "normalize_name",
    "reset",
    "to_segments",
]
