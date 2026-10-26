"""运行环境与路径翻译（Bot 路径 ↔ 协议端路径）。

这个模块是**配置层**的绑定：它把 :class:`~core.pathmap.PathTranslator`
（纯翻译引擎）和 :mod:`config.path_profiles`（预设）接到
``environment.json`` 上，业务代码只需要 :class:`PathBridge`。

配置来源（优先级从高到低）：

1. 环境变量 ``JJ_BOT_ENV`` / ``JJ_BACKEND_ENV``
   （兼容 ``JJ_ONEBOT_ENV`` / ``JJ_NAPCAT_ENV``）
2. ``config/environment.json``
3. ``config/config.json`` 里的同名键
4. 自动探测

核心概念：

- ``bot_env``：Bot 自己跑在哪儿（``windows`` / ``wsl`` / ``linux``）
- ``backend_env``：协议端（NapCat / SnowLuma）跑在哪儿
- ``path_profile``：怎么翻译（``same`` / ``windows_to_wsl`` /
  ``wsl_to_windows`` / ``custom``）
- ``path_map``：额外规则，叠加在 profile 上，最长前缀优先

用法::

    env = EnvConfig.from_mapping({})
    bridge = PathBridge(root, env)
    bridge.to_backend(r"D:\\Code\\x\\a.png")   # -> /mnt/d/Code/x/a.png
    bridge.to_local("/mnt/d/Code/x/a.png")     # -> D:\\Code\\x\\a.png

旧名字 ``to_napcat`` / ``to_bot`` / ``napcat_env`` 仍然可用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable

from core.pathmap import (
    PathRule,
    PathTranslator,
    is_special as _is_special,
    looks_windows as _looks_windows,
)

from .path_profiles import get_profile, normalize_profile

__all__ = [
    "ENV_LABELS",
    "EnvKind",
    "EnvConfig",
    "PathMap",
    "PathBridge",
    "derive_backend_env",
    "derive_profile",
    "detect_env",
    "env_options",
    "load_env_config",
]


class EnvKind(str, Enum):
    WINDOWS = "windows"
    WSL = "wsl"
    LINUX = "linux"

    @classmethod
    def parse(cls, value: Any, default: "EnvKind | None" = None) -> "EnvKind":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        aliases = {
            "win": cls.WINDOWS,
            "windows": cls.WINDOWS,
            "nt": cls.WINDOWS,
            "wsl": cls.WSL,
            "wsl2": cls.WSL,
            "linux": cls.LINUX,
            "posix": cls.LINUX,
            "unix": cls.LINUX,
        }
        if text in aliases:
            return aliases[text]
        if default is not None:
            return default
        raise ValueError(f"无法识别的环境 {value!r}（可用 windows / wsl / linux）")

    @property
    def is_windows(self) -> bool:
        return self is EnvKind.WINDOWS

    @property
    def is_posix(self) -> bool:
        return self in (EnvKind.WSL, EnvKind.LINUX)


ENV_LABELS: dict[EnvKind, str] = {
    EnvKind.WINDOWS: "Windows",
    EnvKind.WSL: "WSL",
    EnvKind.LINUX: "Linux",
}


def env_options() -> list[dict[str, str]]:
    """给网页面板用的环境列表。"""
    return [{"id": kind.value, "label": label} for kind, label in ENV_LABELS.items()]


def detect_env() -> EnvKind:
    """自动探测当前进程所在环境。"""
    if os.name == "nt":
        return EnvKind.WINDOWS
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        version = ""
    if "microsoft" in version or "wsl" in version:
        return EnvKind.WSL
    return EnvKind.LINUX


def derive_backend_env(bot_env: EnvKind, path_profile: str = "") -> EnvKind:
    """没有显式 ``backend_env`` 时，根据 Bot 环境和 profile 推导。"""
    if path_profile == "windows_to_wsl" and bot_env is EnvKind.WINDOWS:
        return EnvKind.WSL
    if path_profile == "wsl_to_windows" and bot_env is EnvKind.WSL:
        return EnvKind.WINDOWS
    return bot_env


def derive_profile(bot_env: EnvKind, backend_env: EnvKind) -> str:
    """没有显式 ``path_profile`` 时，根据两端环境推导。"""
    if bot_env is backend_env:
        return "same"
    if bot_env is EnvKind.WINDOWS and backend_env is EnvKind.WSL:
        return "windows_to_wsl"
    if bot_env is EnvKind.WSL and backend_env is EnvKind.WINDOWS:
        return "wsl_to_windows"
    return "custom"


@dataclass(frozen=True)
class PathMap:
    """用户自定义的前缀映射，叠加在 profile 之上。"""

    from_prefix: str
    to_prefix: str
    note: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "PathMap | None":
        if isinstance(raw, PathMap):
            return raw
        if not isinstance(raw, dict):
            return None
        source = str(raw.get("from") or raw.get("from_prefix") or "").strip()
        target = str(raw.get("to") or raw.get("to_prefix") or "").strip()
        if not source or not target:
            return None
        return cls(source, target, str(raw.get("note") or "").strip())

    def to_dict(self) -> dict[str, str]:
        data = {"from": self.from_prefix, "to": self.to_prefix}
        if self.note:
            data["note"] = self.note
        return data


@dataclass(frozen=True, init=False)
class EnvConfig:
    """环境 + 路径翻译配置。

    构造时兼容旧关键字 ``napcat_env`` / ``onebot_env``；新代码用
    ``backend_env``。写盘只写 ``backend_env``。
    """

    bot_env: EnvKind
    backend_env: EnvKind
    path_profile: str
    wsl_distro: str
    wsl_mount_root: str
    path_map: tuple[PathMap, ...]

    def __init__(
        self,
        bot_env: EnvKind | str | None = None,
        backend_env: EnvKind | str | None = None,
        *,
        napcat_env: EnvKind | str | None = None,
        onebot_env: EnvKind | str | None = None,
        path_profile: str = "",
        wsl_distro: str = "Ubuntu",
        wsl_mount_root: str = "/mnt",
        path_map: Iterable[Any] = (),
    ) -> None:
        bot = EnvKind.parse(bot_env, default=detect_env())

        raw_backend = backend_env
        if raw_backend is None:
            raw_backend = onebot_env
        if raw_backend is None:
            raw_backend = napcat_env
        backend = EnvKind.parse(raw_backend, default=bot) if raw_backend is not None else None

        raw_profile = str(path_profile or "").strip()
        profile = normalize_profile(raw_profile, default="") if raw_profile else ""
        if raw_profile and not profile:
            print(f"[路径] 无法识别的 path_profile={raw_profile!r}，按环境自动推导")

        if backend is None:
            backend = derive_backend_env(bot, profile)
        if not profile:
            profile = derive_profile(bot, backend)

        entries: list[PathMap] = []
        for item in path_map or ():
            parsed = PathMap.from_mapping(item)
            if parsed is not None:
                entries.append(parsed)

        mount = str(wsl_mount_root or "/mnt").strip().replace("\\", "/")
        if not mount.startswith("/"):
            mount = "/" + mount.lstrip("/")
        if not mount.strip("/"):
            mount = "/mnt"

        object.__setattr__(self, "bot_env", bot)
        object.__setattr__(self, "backend_env", backend)
        object.__setattr__(self, "path_profile", profile)
        object.__setattr__(self, "wsl_distro", str(wsl_distro or "Ubuntu").strip() or "Ubuntu")
        object.__setattr__(self, "wsl_mount_root", mount)
        object.__setattr__(self, "path_map", tuple(entries))

    # ── 构造 / 序列化 ──
    @classmethod
    def from_mapping(cls, raw: dict | None, *, default_bot: EnvKind | None = None) -> "EnvConfig":
        raw = raw or {}
        bot = EnvKind.parse(
            os.environ.get("JJ_BOT_ENV")
            or raw.get("bot_env")
            or raw.get("bot_environment"),
            default=default_bot or detect_env(),
        )
        backend_raw = (
            os.environ.get("JJ_BACKEND_ENV")
            or os.environ.get("JJ_ONEBOT_ENV")
            or os.environ.get("JJ_NAPCAT_ENV")
            or raw.get("backend_env")
            or raw.get("onebot_env")
            or raw.get("napcat_env")
            or raw.get("napcat_environment")
        )
        backend = (
            EnvKind.parse(backend_raw, default=bot)
            if backend_raw not in (None, "")
            else None
        )
        profile = (
            raw.get("path_profile")
            or raw.get("profile")
            or raw.get("path_map_profile")
            or ""
        )
        return cls(
            bot_env=bot,
            backend_env=backend,
            path_profile=str(profile or ""),
            wsl_distro=str(raw.get("wsl_distro") or "Ubuntu"),
            wsl_mount_root=str(raw.get("wsl_mount_root") or "/mnt"),
            path_map=raw.get("path_map") or (),
        )

    def to_dict(self) -> dict:
        return {
            "bot_env": self.bot_env.value,
            "backend_env": self.backend_env.value,
            "path_profile": self.path_profile,
            "wsl_distro": self.wsl_distro,
            "wsl_mount_root": self.wsl_mount_root,
            "path_map": [item.to_dict() for item in self.path_map],
        }

    # ── 兼容属性 ──
    @property
    def napcat_env(self) -> EnvKind:
        """旧名字：协议端所在环境。"""
        return self.backend_env

    @property
    def onebot_env(self) -> EnvKind:
        """旧名字：协议端所在环境。"""
        return self.backend_env

    # ── 翻译器 ──
    @cached_property
    def translator(self) -> PathTranslator:
        """按当前 profile + path_map 构建的翻译器（按需缓存）。"""
        profile = get_profile(self.path_profile)
        extra = [
            PathRule(item.from_prefix, item.to_prefix, item.note)
            for item in self.path_map
        ]
        return PathTranslator(profile.rules(mount_root=self.wsl_mount_root, extra=extra))


_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")
_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")
_WSL_UNC_RE = re.compile(r"^\\\\wsl(?:\$|\.localhost)\\([^\\]+)\\(.*)$", re.IGNORECASE)


class PathBridge:
    """把「Bot 本机路径」和「协议端看到的路径」互相转换。

    实例是**长期持有**的：配置热加载时调用 :meth:`reconfigure`，
    不要重新 new 一个，否则 ``ImageResolver`` 等持有者会拿到旧对象。
    """

    def __init__(self, root: str | Path, env: EnvConfig | None = None):
        self.root = Path(root).resolve()
        self.env = env or EnvConfig()
        self._warned: set[str] = set()
        self._warn_profile()

    def reconfigure(self, env: EnvConfig) -> "PathBridge":
        """原地换配置（热加载用），保留对象身份。"""
        self.env = env
        self._warn_profile()
        return self

    # ── 公共 API ──
    @staticmethod
    def is_special(value: Any) -> bool:
        return _is_special(str(value))

    @property
    def translator(self) -> PathTranslator:
        return self.env.translator

    def expand(self, value: str | os.PathLike[str], base: str | Path | None = None) -> Path:
        """把相对路径 / ``~`` / 旧配置里的跨环境路径统一成本机绝对路径。

        这里保留了一个**兼容 shim**：如果 Bot 在 Windows 却拿到 ``/mnt/d/...``，
        或 Bot 在 WSL 却拿到 ``D:\\...``，说明是旧配置直接写的协议端路径，
        会先换成本机路径。Bot 自己的原生路径不会触发这个分支。
        """
        text = str(value or "").strip()
        if _is_special(text):
            raise ValueError(f"不是本地路径：{text}")
        text = os.path.expandvars(os.path.expanduser(text))

        if self.env.bot_env.is_windows:
            # 先走规则（支持自定义挂载根），没命中再按旧配置的 /mnt/<盘符> 兜底。
            # 只有以 / 开头的串才可能是协议端路径；盘符 / UNC 已经是本机路径。
            if text.startswith("/"):
                translated = self.translator.to_local(text)
                if translated != text:
                    text = translated
                elif _WSL_MOUNT_RE.match(text):
                    text = self.wsl_to_windows(text)
        elif self.env.bot_env.is_posix and _looks_windows(text):
            translated = self.translator.to_backend(text)
            if translated != text:
                text = translated
            else:
                text = self.windows_to_wsl(text)

        path = Path(text)
        if not path.is_absolute():
            path = (Path(base) if base is not None else self.root) / path
        return path.resolve(strict=False)

    def to_backend(self, value: str | os.PathLike[str], *, base: str | Path | None = None) -> str:
        """本机路径 → 协议端路径。URL / base64 原样返回。"""
        text = str(value or "").strip()
        if not text or _is_special(text):
            return text

        # 先按原始字符串匹配：自定义规则可以覆盖「还没展开」的相对 / 异环境路径
        translated = self.translator.to_backend(text)
        if translated != text:
            return translated

        path = self.expand(text, base=base)
        return self.translator.to_backend(str(path))

    def to_local(self, value: str | os.PathLike[str], *, base: str | Path | None = None) -> Path:
        """协议端路径 → 本机路径。仅用于本地文件，不要传 URL。"""
        text = str(value or "").strip()
        if _is_special(text):
            raise ValueError(f"不是本地路径：{text}")

        translated = self.translator.to_local(text)
        if translated == text:
            # 兼容旧配置 / 旧调用：profile 是 same 时也认 /mnt 和盘符
            if self.env.bot_env.is_windows and _WSL_MOUNT_RE.match(text):
                translated = self.wsl_to_windows(text)
            elif self.env.bot_env is EnvKind.WSL and _looks_windows(text):
                translated = self.windows_to_wsl(text)

        path = Path(translated)
        if not path.is_absolute():
            path = (Path(base) if base is not None else self.root) / path
        return path.resolve(strict=False)

    # ── 旧名字（保留给现有调用 / 测试） ──
    def to_napcat(self, value: str | os.PathLike[str], *, base: str | Path | None = None) -> str:
        return self.to_backend(value, base=base)

    def to_bot(self, value: str | os.PathLike[str], *, base: str | Path | None = None) -> Path:
        return self.to_local(value, base=base)

    def to_runtime(self, value: str | os.PathLike[str], *, base: str | Path | None = None) -> str:
        return self.to_backend(value, base=base)

    # ── 具体转换（不依赖 profile，供兼容 shim / 工具使用） ──
    def windows_to_wsl(self, value: str) -> str:
        text = str(value or "").strip()
        unc = _WSL_UNC_RE.match(text)
        if unc:
            return "/" + unc.group(2).replace("\\", "/").strip("/")
        match = _DRIVE_RE.match(text)
        if not match:
            return text.replace("\\", "/")
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/").strip("/")
        mount = self.env.wsl_mount_root.rstrip("/") or "/mnt"
        return f"{mount}/{drive}" + (f"/{rest}" if rest else "")

    def wsl_to_windows(self, value: str) -> str:
        text = str(value or "").strip()
        match = _WSL_MOUNT_RE.match(text)
        if not match:
            # 不是 /mnt/<drive>/... 的路径（例如 /home/x）无法映射到 Windows
            return text
        drive = match.group(1).upper()
        rest = (match.group(2) or "").replace("/", "\\").strip("\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"

    # ── path_map 单独匹配（兼容旧内部方法） ──
    def _map_forward(self, text: str) -> str | None:
        rules = [
            PathRule(item.from_prefix, item.to_prefix, item.note)
            for item in self.env.path_map
        ]
        if not rules:
            return None
        translated = PathTranslator(rules).to_backend(text)
        return translated if translated != text else None

    def _map_reverse(self, text: str) -> str | None:
        rules = [
            PathRule(item.from_prefix, item.to_prefix, item.note)
            for item in self.env.path_map
        ]
        if not rules:
            return None
        translated = PathTranslator(rules).to_local(text)
        return translated if translated != text else None

    # ── 警告只打一次 ──
    def _warn_profile(self) -> None:
        env = self.env
        if env.path_profile == "same" and env.bot_env is not env.backend_env:
            self._warn_once(
                f"same:{env.bot_env.value}:{env.backend_env.value}",
                "path_profile=same，但 Bot 与协议端环境不同；"
                "如果两端路径不一致，请改成 windows_to_wsl / wsl_to_windows / custom。",
            )
        if (
            env.path_profile == "custom"
            and env.bot_env is not env.backend_env
            and not env.path_map
        ):
            self._warn_once(
                f"custom:{env.bot_env.value}:{env.backend_env.value}",
                "path_profile=custom 但没有 path_map，路径会原样发送；"
                "请在 environment.json 或「路径映射」页添加规则。",
            )
        if env.path_profile == "windows_to_wsl" and env.bot_env is not EnvKind.WINDOWS:
            self._warn_once(
                f"win2wsl:{env.bot_env.value}",
                "path_profile=windows_to_wsl，但 Bot 不在 Windows；规则可能不适用。",
            )
        if env.path_profile == "wsl_to_windows" and env.bot_env is not EnvKind.WSL:
            self._warn_once(
                f"wsl2win:{env.bot_env.value}",
                "path_profile=wsl_to_windows，但 Bot 不在 WSL；规则可能不适用。",
            )

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        print(f"[路径] {message}")


def load_env_config(*sources: dict | None, default_bot: EnvKind | None = None) -> EnvConfig:
    """按顺序合并多个配置来源（后者只补前者没有的键）。"""
    merged: dict = {}
    # 前面的来源优先：environment.json > config.json
    for source in [item for item in sources if item]:
        for key, value in source.items():
            if key in (
                "bot_env",
                "bot_environment",
                "backend_env",
                "onebot_env",
                "napcat_env",
                "napcat_environment",
                "path_profile",
                "profile",
                "path_map_profile",
                "wsl_distro",
                "wsl_mount_root",
                "path_map",
            ):
                merged.setdefault(key, value)
    return EnvConfig.from_mapping(merged, default_bot=default_bot)
