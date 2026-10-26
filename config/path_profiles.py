"""路径翻译预设（``path_profile``）。

一个 profile 就是「按什么规则把 Bot 路径翻译成协议端路径」的预设。
真正的匹配逻辑在 :mod:`core.pathmap`；这里只负责**生成规则**，不负责翻译。

四个预设：

===============  ==================================================
``same``         同机 / 共享挂载，路径原样使用
``windows_to_wsl``  Bot 在 Windows，协议端在 WSL：``D:\\x`` → ``/mnt/d/x``
``wsl_to_windows``  Bot 在 WSL，协议端在 Windows：``/mnt/d/x`` → ``D:\\x``
``custom``       不做任何假设，只用用户配置的 ``path_map``
===============  ==================================================

``path_map`` 永远叠加在预设之上，按**最长前缀**优先，所以可以只覆盖
``D:\\share`` 这种特殊目录，而不影响其它盘符。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from core.pathmap import PathRule

__all__ = [
    "PROFILES",
    "PathProfile",
    "get_profile",
    "normalize_profile",
    "path_profile_options",
]

_DRIVES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _mount_root(value: Any) -> str:
    text = str(value or "/mnt").strip().replace("\\", "/")
    text = "/" + text.strip("/")
    return text if text != "/" else "/mnt"


def _windows_to_wsl_rules(mount_root: Any = "/mnt") -> list[PathRule]:
    root = _mount_root(mount_root)
    return [PathRule(f"{drive}:\\", f"{root}/{drive.lower()}") for drive in _DRIVES]


def _wsl_to_windows_rules(mount_root: Any = "/mnt") -> list[PathRule]:
    root = _mount_root(mount_root)
    return [PathRule(f"{root}/{drive.lower()}", f"{drive}:\\") for drive in _DRIVES]


@dataclass(frozen=True)
class PathProfile:
    """一个路径翻译预设。"""

    id: str
    label: str
    description: str
    needs_mount_root: bool = False
    notes: tuple[str, ...] = ()

    def rules(
        self,
        *,
        mount_root: Any = "/mnt",
        extra: Iterable[Any] = (),
    ) -> list[PathRule]:
        """预设规则 + 用户 ``path_map``（后者叠加，最长前缀优先）。"""
        rules: list[PathRule] = []
        if self.id == "windows_to_wsl":
            rules.extend(_windows_to_wsl_rules(mount_root))
        elif self.id == "wsl_to_windows":
            rules.extend(_wsl_to_windows_rules(mount_root))
        for item in extra or ():
            rule = item if isinstance(item, PathRule) else PathRule.from_mapping(item)
            if rule is not None:
                rules.append(rule)
        return rules

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "needs_mount_root": self.needs_mount_root,
            "notes": list(self.notes),
        }


SAME = PathProfile(
    id="same",
    label="同机 / 共享挂载",
    description="Bot 和协议端看到的是同一套路径，原样发送。",
    notes=(
        "Bot 与 NapCat / SnowLuma 在同一台机器，或挂载点完全一致时使用。",
        "相对路径仍会先按项目根目录展开。",
    ),
)

WINDOWS_TO_WSL = PathProfile(
    id="windows_to_wsl",
    label="Windows → WSL",
    description="Bot 在 Windows，协议端在 WSL：盘符路径转成 /mnt/<盘符>。",
    needs_mount_root=True,
    notes=(
        "只转换盘符路径；http(s) / base64 / data URI 原样返回。",
        "\\wsl$\\<distro>\\... 这类 UNC 路径也会换成 WSL 内部路径。",
    ),
)

WSL_TO_WINDOWS = PathProfile(
    id="wsl_to_windows",
    label="WSL → Windows",
    description="Bot 在 WSL，协议端在 Windows：/mnt/<盘符> 转回盘符路径。",
    needs_mount_root=True,
    notes=(
        "只有 /mnt/<盘符>/... 能自动转；/home/... 这类路径需要自定义规则。",
    ),
)

CUSTOM = PathProfile(
    id="custom",
    label="自定义",
    description="不自动推导，完全按下面的规则表翻译。",
    notes=(
        "没有命中任何规则时路径原样发送。",
        "规则按最长前缀优先；例如 D:\\share 会覆盖 D:\\ 的盘符规则。",
    ),
)

PROFILES: dict[str, PathProfile] = {
    profile.id: profile
    for profile in (SAME, WINDOWS_TO_WSL, WSL_TO_WINDOWS, CUSTOM)
}

_ALIASES: dict[str, str] = {
    "": "same",
    "none": "same",
    "identity": "same",
    "raw": "same",
    "same_host": "same",
    "local": "same",
    "same_machine": "same",
    "win2wsl": "windows_to_wsl",
    "windows_wsl": "windows_to_wsl",
    "windows_to_wsl": "windows_to_wsl",
    "wsl2windows": "wsl_to_windows",
    "wsl_windows": "wsl_to_windows",
    "wsl_to_windows": "wsl_to_windows",
    "manual": "custom",
    "path_map": "custom",
    "pathmap": "custom",
    "custom": "custom",
}


def normalize_profile(value: Any, *, default: str = "same") -> str:
    """把别名 / 大小写 / 连字符统一成注册表里的 id。

    ``default=""`` 时无法识别返回空串（供面板校验使用），不会静默改成 same。
    """
    if isinstance(value, PathProfile):
        return value.id
    text = str(value or "").strip().lower().replace(" ", "")
    key = text.replace("-", "_")
    if key in PROFILES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    return default


def get_profile(value: Any = None) -> PathProfile:
    """取 profile；未知值回落到 ``same``。"""
    return PROFILES[normalize_profile(value, default="same")]


def path_profile_options() -> list[dict[str, Any]]:
    """给网页面板用的预设列表（顺序稳定：same 在前）。"""
    return [profile.to_dict() for profile in PROFILES.values()]
