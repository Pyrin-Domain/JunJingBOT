"""表情包库：按名字找表情，渲染成 NapCat 消息段 / CQ 码。

数据格式跟 Meme.json 一致，分三类：

- ``faces``：QQ 内置小表情，只有数字 id，走 ``[CQ:face,id=…]``；
- ``mfaces``：QQ 商城表情包（就是群里那种大表情），需要
  ``emoji_package_id`` + ``emoji_id`` + ``key``，走 ``mface`` 消息段；
- ``stickers``：本地图片，走 ``[CQ:image,file=…]``。

模型只会写名字（``<meme>嘬嘬</meme>``），而名字是从提示词里的目录抄的，
很容易多写一个「表情」、少写一个字。所以匹配按三级退让：

    完全一致 → 名字互相包含 → 描述里命中

三级都没命中就返回 None —— 宁可这一轮不发，也不要瞎发一个不相关的表情
（发错表情比不发更尴尬）。

名字与数据的对应关系全部来自配置文件，改表情包不用改代码。
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


@dataclass(frozen=True)
class MemeEntry:
    """一条表情：名字、描述、类型和发送所需的数据。"""

    name: str
    describe: str = ""
    kind: str = "mface"          # face / mface / sticker
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
        return {"type": "mface", "data": data}

    def cq(self) -> str:
        """转成 CQ 码（可以混在普通文本里一起发）。"""
        if self.kind == "face":
            return f"[CQ:face,id={cq_escape(self.data.get('id', ''))}]"
        if self.kind == "sticker":
            file = self.data.get("file") or self.data.get("path") or ""
            return f"[CQ:image,file={cq_escape(file)}]"
        parts = []
        for key in ("emoji_id", "emoji_package_id", "key", "summary"):
            value = self.data.get(key)
            if value in (None, ""):
                continue
            parts.append(f"{key}={cq_escape(value)}")
        return f"[CQ:mface,{','.join(parts)}]" if parts else ""

    def label(self) -> str:
        return f"<{self.name}>"


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
        for kind in ("faces", "mfaces", "stickers"):
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
    "image": (("file", "file"), ("summary", "summary")),
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


def to_segments(text: Any) -> Optional[list[dict]]:
    """把含 mface 的 CQ 码字符串切成消息段数组；不含则返回 None。

    为什么要转：mface 的 ``summary`` 带方括号，作为 CQ 码交给 NapCat 解析时
    容易被截断；直接发消息段数组不经过 CQ 码解析，最稳。其余 CQ 码顺手一起
    转过去，保证「文字 + 表情回应 + 表情包」混排时的顺序不乱。

    返回 None 表示「这条没有表情包」——调用方继续按原来的字符串发送，
    旧路径完全不受影响。
    """
    raw = str(text if text is not None else "")
    if not has_mface(raw):
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


def configure(path: str | Path | None = None, *, enable: bool = True) -> MemeLibrary:
    """配置并重载默认库（启动 / 面板改配置后调用）。"""
    global _default_library, _default_path, _default_enabled
    target = _resolve_path(path)
    _default_path = str(target)
    _default_enabled = bool(enable)
    _default_library = MemeLibrary.load(target) if enable else MemeLibrary()
    if enable:
        logger.info("[表情] 已加载 %d 个表情（%s）", len(_default_library), target)
    return _default_library


def get_library() -> MemeLibrary:
    if _default_library is None:
        configure()
    return _default_library or MemeLibrary()


def is_enabled() -> bool:
    if _default_library is None:
        configure()
    return _default_enabled


def reset() -> None:
    """测试用：清掉单例。"""
    global _default_library, _default_path, _default_enabled
    _default_library = None
    _default_path = ""
    _default_enabled = True


__all__ = [
    "MemeEntry",
    "MemeLibrary",
    "configure",
    "cq_escape",
    "cq_unescape",
    "default_path",
    "get_library",
    "has_mface",
    "is_enabled",
    "normalize_name",
    "reset",
    "to_segments",
]
