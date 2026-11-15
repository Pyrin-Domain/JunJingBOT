"""Agent 自己的口头禅库。

口头禅放在 JSON 文件里，不写死在提示词中。推荐格式：

.. code-block:: json

    [
      {
        "context": "那当然啦~",
        "describe": "被夸可爱、被夸厉害时说；表达开心又有点得意"
      },
      {
        "context": "你猜",
        "describe": "被问到是不是机器人时用；不正面回答，带点调皮"
      }
    ]

``context`` 是口头禅内容（实际可以说的话），``describe`` 是这句口头禅的
描述：什么时候用、什么意思。也兼容顶层 ``{"catchphrases": [...]}``、单对象，
以及 ``phrase`` / ``text`` 等常见别名；文件缺失、为空或 JSON 写坏时只返回
空列表，不影响 Bot 启动。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CATCHPHRASE_FILE_DEFAULT = "agent/catchphrases.json"

# 文件里的 context 是「口头禅内容」，describe 是「什么时候用 / 什么意思」。
_CONTENT_KEYS = (
    "context",
    "phrase",
    "text",
    "say",
    "content",
    "catchphrase",
    "reply",
)
_DESCRIBE_KEYS = ("describe", "description", "usage", "meaning", "when", "note")
# describe 里用引号举的例子，直接当成轻量触发词（如「在吗？」「君景！」）。
_QUOTED_TRIGGER_RE = re.compile(r"[「『\"“]([^」』\"”\n]{1,40})[」』\"”]")
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]+\]")


@dataclass(frozen=True)
class Catchphrase:
    """一条口头禅：内容（context）+ 使用说明（describe）。"""

    context: str = ""
    describe: str = ""

    @property
    def content(self) -> str:
        """口头禅正文；``context`` 是文件里的字段名，保留给调用方兼容。"""
        return self.context

    @property
    def triggers(self) -> tuple[str, ...]:
        """从 describe 的引号例子里取触发词；没有例子就返回空。"""
        if not self.describe:
            return ()
        seen: list[str] = []
        for match in _QUOTED_TRIGGER_RE.findall(self.describe):
            text = match.strip()
            if text and text not in seen:
                seen.append(text)
        return tuple(seen)

    def prompt_line(self) -> str:
        """给提示词用的单行格式。"""
        if self.context and self.describe:
            return f"- 口头禅：「{self.context}」｜说明：{self.describe}"
        return f"- 口头禅：「{self.context or self.describe}」"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "、".join(part for item in value if (part := _text(item)))
    return str(value).strip()


def _first_text(entry: dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = _text(entry.get(key))
        if value:
            return value
    return ""


def _iter_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("catchphrases", "items", "data", "phrases"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(key in data for key in _CONTENT_KEYS + _DESCRIBE_KEYS):
            return [data]
        # 也兼容 {"那当然啦~": "被夸可爱时说"} 这种简单映射
        if data and all(isinstance(value, (str, list, tuple)) for value in data.values()):
            return [{"context": key, "describe": value} for key, value in data.items()]
    return []


def parse_catchphrases(data: Any) -> list[Catchphrase]:
    """把 JSON 数据解析成去重后的口头禅列表。

    ``context`` 读成口头禅正文，``describe`` 读成使用说明；字符串项按
    「只有正文、没有说明」处理。
    """
    entries: list[Catchphrase] = []
    seen: set[tuple[str, str]] = set()
    for raw in _iter_items(data):
        if isinstance(raw, str):
            content, describe = raw.strip(), ""
        elif isinstance(raw, dict):
            content = _first_text(raw, _CONTENT_KEYS)
            describe = _first_text(raw, _DESCRIBE_KEYS)
            # 只写了 describe、没写 context 的旧写法，至少别把这句话丢掉。
            if not content and describe:
                content, describe = describe, ""
        else:
            continue

        if not content:
            continue
        key = (content, describe)
        if key in seen:
            continue
        seen.add(key)
        entries.append(Catchphrase(content, describe))
    return entries


def load_catchphrases(path: str | Path) -> list[Catchphrase]:
    """读取口头禅 JSON；文件不存在 / 写坏时安静地返回空列表。"""
    file = Path(path)
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[口头禅] 读取 %s 失败：%s", file, exc)
        return []
    return parse_catchphrases(data)


def catchphrase_file_from_settings(settings=None) -> Path | None:
    """按 ``agent.catchphrase_file`` 解析出实际路径；关闭时返回 ``None``。"""
    section: dict = {}
    if settings is not None:
        try:
            section = settings.section("agent") or {}
        except Exception:  # noqa: BLE001 - 旧配置可能没有 agent 段
            section = {}

    if "catchphrase_file" in section:
        value = section.get("catchphrase_file")
    elif "catchphrases_file" in section:
        value = section.get("catchphrases_file")
    else:
        value = CATCHPHRASE_FILE_DEFAULT
    if value is False:
        return None
    text = str(value or "").strip()
    if not text:
        return None

    path = Path(text).expanduser()
    if not path.is_absolute():
        root = getattr(getattr(settings, "paths", None), "root", None)
        base = Path(root) if root else Path(__file__).resolve().parent.parent
        path = base / path
    return path


def load_catchphrases_for_settings(settings=None) -> list[Catchphrase]:
    """读取当前配置指向的口头禅文件；路径为空 / 关闭时返回空列表。"""
    path = catchphrase_file_from_settings(settings)
    return load_catchphrases(path) if path is not None else []


def _normalize_trigger_text(text: Any) -> str:
    value = _CQ_CODE_RE.sub("", str(text or ""))
    return re.sub(r"\s+", "", value)


def catchphrase_hint(user_message: str, entries: Iterable[Catchphrase]) -> str:
    """当前消息命中 describe 里的引号例子时，给本轮追加一条口头禅提示。

    描述里写「在吗？」「君景！」这种例子时，用户消息只要包含其中一个，
    就在本轮用户消息末尾点名提醒一次，避免小模型在长提示词里忽略口头禅库。
    """
    norm = _normalize_trigger_text(user_message)
    if not norm:
        return ""

    best: tuple[int, Catchphrase, str] | None = None
    for entry in entries:
        for trigger in entry.triggers:
            target = _normalize_trigger_text(trigger)
            if not target:
                continue
            if target == norm:
                score = 1000 + len(target)
            elif target in norm:
                score = 500 + len(target)
            elif len(norm) >= 2 and norm in target:
                score = 300 + len(norm)
            else:
                continue
            if best is None or score > best[0]:
                best = (score, entry, trigger)

    if best is None:
        return ""
    _, entry, trigger = best
    return (
        f"- 当前消息命中了口头禅「{entry.context}」的触发例子「{trigger}」："
        f"{entry.describe}\n"
        "  情境确实吻合时优先用这条口头禅回应，不要用「在呢」「怎么啦」"
        "之类的普通应答盖过去；不合适就正常说。"
    )


def catalog_lines(entries: Iterable[Catchphrase], limit: int = 0) -> list[str]:
    """转成提示词目录行；``limit<=0`` 表示全部。"""
    rows = list(entries)
    if limit > 0:
        rows = rows[:limit]
    return [entry.prompt_line() for entry in rows]


def render_catalog(entries: Iterable[Catchphrase], limit: int = 0) -> str:
    """渲染成可以直接塞进提示词的文本块。"""
    lines = catalog_lines(entries, limit)
    if not lines:
        return "（暂无配置；不要自己编口头禅，也不要为了口头禅硬凑。）"
    return "\n".join(lines)


__all__ = [
    "CATCHPHRASE_FILE_DEFAULT",
    "Catchphrase",
    "catchphrase_file_from_settings",
    "catchphrase_hint",
    "catalog_lines",
    "load_catchphrases",
    "load_catchphrases_for_settings",
    "parse_catchphrases",
    "render_catalog",
]
