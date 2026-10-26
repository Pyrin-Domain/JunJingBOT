"""路径翻译引擎（纯逻辑，不读配置、不碰文件系统）。

这个模块只回答一个问题：**一串路径字符串按规则应该变成什么**。
它不知道 NapCat / SnowLuma，也不知道 Bot 跑在 Windows 还是 WSL —— 那些
属于 :mod:`config.environment` / :mod:`config.path_profiles`。

规则匹配约定：

- **最长前缀优先**，与规则列表顺序无关（同长度时先出现的优先）。
- **边界感知**：``/mnt/d`` 不会匹配 ``/mnt/database``。
- Windows 盘符 / UNC 的 ``source`` 大小写不敏感，但**保留剩余部分的原始大小写**
  （WSL 里区分大小写，不能把 ``Code`` 变成 ``code``）。
- ``\\`` 和 ``/`` 在比较时等价；输出分隔符跟随 ``target`` 的风格。
- URL / base64 / data URI 原样返回，不参与翻译。

示例::

    translator = PathTranslator([
        PathRule("D:\\\\", "/mnt/d"),
        PathRule("D:\\\\share", "/data/share"),
    ])
    translator.to_backend(r"D:\\Code\\a.png")   # -> /mnt/d/Code/a.png
    translator.to_backend(r"D:\\share\\a.png")  # -> /data/share/a.png
    translator.to_local("/mnt/d/Code/a.png")    # -> D:\\Code\\a.png
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "PathRule",
    "PathTranslator",
    "is_special",
    "looks_windows",
    "match_prefix",
    "SPECIAL_PREFIXES",
]

SPECIAL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "base64://",
    "file://",
    "data:",
    "//",
)

_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:[\\/]|$)")
_UNC_RE = re.compile(r"^\\\\")


def is_special(value: Any) -> bool:
    """URL / base64 / data URI 等不应该被当成文件路径的值。"""
    text = str(value or "").strip()
    return text.lower().startswith(SPECIAL_PREFIXES)


def looks_windows(value: Any) -> bool:
    """是否是 Windows 盘符路径或 UNC 路径。"""
    text = str(value or "")
    return bool(_DRIVE_RE.match(text)) or bool(_UNC_RE.match(text))


def _canonical(text: str) -> tuple[str, bool]:
    """比较用的规范形式：统一分隔符；Windows 路径折叠大小写。"""
    raw = str(text or "")
    windows = looks_windows(raw)
    canon = raw.replace("\\", "/")
    if windows:
        canon = canon.casefold()
    return canon, windows


def match_prefix(text: Any, prefix: Any) -> tuple[bool, int]:
    """判断 ``text`` 是否以 ``prefix`` 开头（边界感知）。

    返回 ``(matched, rest_start)``；``rest_start`` 是原始 ``text`` 中剩余
    部分的起始下标（不是规范形式的下标 —— 两者长度一致）。
    """
    text = str(text or "").strip()
    prefix = str(prefix or "").strip()
    if not text or not prefix:
        return False, 0

    text_canon, _ = _canonical(text)
    prefix_canon, _ = _canonical(prefix)
    base = prefix_canon.rstrip("/")

    if not base:
        # prefix 是 "/" 或 "\\" 这类根
        if text_canon.startswith("/"):
            return True, 1
        return False, 0
    if text_canon == base or text_canon == prefix_canon:
        return True, len(text)
    if text_canon.startswith(base + "/"):
        return True, len(base) + 1
    return False, 0


def join_target(target: str, rest: str) -> str:
    """把 ``rest`` 接到 ``target`` 后面，分隔符跟随 target 的风格。"""
    target = str(target or "").strip()
    separator = "\\" if looks_windows(target) else "/"
    cleaned = str(rest or "").replace("\\", "/").strip("/")
    if not cleaned:
        return target
    base = target.rstrip("/\\")
    if not base:
        return separator + cleaned.replace("/", separator)
    return base + separator + cleaned.replace("/", separator)


@dataclass(frozen=True)
class PathRule:
    """一条前缀映射：``source`` → ``target``。

    ``note`` 只用于面板展示，不参与匹配。
    """

    source: str
    target: str
    note: str = ""

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        target = str(self.target or "").strip()
        if not source:
            raise ValueError("PathRule.source 不能为空")
        if not target:
            raise ValueError("PathRule.target 不能为空")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "note", str(self.note or "").strip())

    @classmethod
    def from_mapping(cls, raw: Any) -> "PathRule | None":
        """从 ``{"from": ..., "to": ..., "note": ...}`` 解析；缺字段返回 None。"""
        if isinstance(raw, PathRule):
            return raw
        if not isinstance(raw, dict):
            return None
        source = str(raw.get("from") or raw.get("source") or "").strip()
        target = str(raw.get("to") or raw.get("target") or "").strip()
        if not source or not target:
            return None
        return cls(source, target, str(raw.get("note") or ""))

    def reversed(self) -> "PathRule":
        return PathRule(self.target, self.source, self.note)

    def to_dict(self) -> dict[str, str]:
        data = {"from": self.source, "to": self.target}
        if self.note:
            data["note"] = self.note
        return data


class PathTranslator:
    """一组规则的双向翻译器。

    ``style`` 只影响**没有命中任何规则**时的分隔符规范化：

    - ``"auto"``（默认）：原样返回，不猜环境
    - ``"windows"``：``/`` → ``\\``
    - ``"posix"``：``\\`` → ``/``
    """

    def __init__(self, rules: Iterable[PathRule] = (), *, style: str = "auto"):
        cleaned: list[PathRule] = []
        for item in rules or ():
            rule = item if isinstance(item, PathRule) else PathRule.from_mapping(item)
            if rule is not None:
                cleaned.append(rule)
        self.rules: tuple[PathRule, ...] = tuple(cleaned)
        self.style = style if style in ("auto", "windows", "posix") else "auto"
        self._forward = tuple(sorted(self.rules, key=lambda rule: len(rule.source), reverse=True))
        self._reverse = tuple(
            sorted((rule.reversed() for rule in self.rules), key=lambda rule: len(rule.source), reverse=True)
        )

    # ── 主接口 ──
    def to_backend(self, value: Any) -> str:
        """Bot 本机路径 → 协议端路径。"""
        return self._translate(value, self._forward)

    def to_local(self, value: Any) -> str:
        """协议端路径 → Bot 本机路径。"""
        return self._translate(value, self._reverse)

    def reverse(self) -> "PathTranslator":
        """返回一个规则方向相反的翻译器（用于只需要单向规则的场景）。"""
        return PathTranslator((rule.reversed() for rule in self.rules), style=self.style)

    def explain(self, value: Any, *, direction: str = "backend") -> dict[str, Any]:
        """给面板预览用：返回输入、输出、命中的规则。"""
        text = str(value or "").strip()
        special = is_special(text)
        rules = self._reverse if str(direction).lower() in ("local", "bot", "reverse") else self._forward
        result: dict[str, Any] = {
            "input": text,
            "output": text,
            "matched": False,
            "special": special,
            "rule": None,
        }
        if special or not text:
            return result
        for rule in rules:
            matched, start = match_prefix(text, rule.source)
            if matched:
                result["output"] = join_target(rule.target, text[start:])
                result["matched"] = True
                result["rule"] = rule.to_dict()
                return result
        result["output"] = self._normalize(text)
        return result

    # ── 内部 ──
    def _translate(self, value: Any, rules: tuple[PathRule, ...]) -> str:
        text = str(value or "").strip()
        if not text or is_special(text):
            return text
        for rule in rules:
            matched, start = match_prefix(text, rule.source)
            if matched:
                return join_target(rule.target, text[start:])
        return self._normalize(text)

    def _normalize(self, text: str) -> str:
        if self.style == "windows":
            return text.replace("/", "\\")
        if self.style == "posix":
            return text.replace("\\", "/")
        return text
