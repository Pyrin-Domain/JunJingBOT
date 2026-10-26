"""OneBot 连接配置（config.json / sender_config.json）。

只负责解析字段，不负责建立连接；连接实现见 ``transport.connection``。

后端（NapCat / SnowLuma）的差异集中在 :mod:`config.backends`：这里只保存
``backend`` 字段并暴露 ``backend_profile`` / ``history_anchor``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.jsonstore import read_json

from .backends import DEFAULT_BACKEND, BackendProfile, get_backend, normalize_backend

MODE_FORWARD = "ws"
MODE_REVERSE = "ws_re"
DEFAULT_MODE = MODE_REVERSE

DEFAULT_FORWARD_HOST = "127.0.0.1"
DEFAULT_FORWARD_PORT = 3001
DEFAULT_REVERSE_HOST = "127.0.0.1"
DEFAULT_REVERSE_PORT = 8085

_FORWARD_ALIASES = {"ws", "ws_forward", "forward", "client", "正向"}
_REVERSE_ALIASES = {"ws_re", "ws_reverse", "ws_rev", "reverse", "server", "反向"}
_BACKEND_KEYS = ("backend", "protocol", "adapter")


def normalize_mode(value: Any, *, default: str = DEFAULT_MODE) -> str:
    if value is None:
        return default
    key = str(value).strip().lower()
    if key in _FORWARD_ALIASES:
        return MODE_FORWARD
    if key in _REVERSE_ALIASES:
        return MODE_REVERSE
    print(f"[WS] 无法识别的 type={value!r}，按 {default} 处理（可用 ws / ws_re）")
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raw_backend(raw: dict) -> Any:
    for key in _BACKEND_KEYS:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return value
    return None


class ConnectionSettings:
    """一个 OneBot 后端连接的配置。"""

    def __init__(
        self,
        raw: dict | None = None,
        *,
        path: str | Path | None = None,
        required: bool = False,
        default_backend: str | None = None,
    ):
        self.path = Path(path) if path is not None else Path("<memory>")
        self._raw = dict(raw or {})
        self.type = normalize_mode(self._raw.get("type"))
        self.access_token = str(self._raw.get("access_token") or "")

        self.backend = normalize_backend(
            _raw_backend(self._raw),
            default=normalize_backend(default_backend, default=DEFAULT_BACKEND),
        )
        self.backend_profile: BackendProfile = get_backend(self.backend)

        self.ws_host = str(
            self._raw.get("ws_host") or self.backend_profile.default_ws_host
        )
        self.ws_port = _as_int(
            self._raw.get("ws_port"), self.backend_profile.default_ws_port
        )
        self.ws_reverse_host = str(
            self._raw.get("ws_reverse_host")
            or self.backend_profile.default_reverse_host
        )
        self.ws_reverse_port = _as_int(
            self._raw.get("ws_reverse_port"),
            self.backend_profile.default_reverse_port,
        )
        self.strict_token = bool(self._raw.get("ws_reverse_strict_token", False))

        if not self.access_token and (required or self.is_forward):
            raise ValueError(f"{self.path} 中未找到 access_token")

    # ── 构造 ──
    @classmethod
    def from_mapping(
        cls,
        raw: dict,
        *,
        path: str | Path | None = None,
        required: bool = False,
        default_backend: str | None = None,
    ) -> "ConnectionSettings":
        return cls(
            raw,
            path=path,
            required=required,
            default_backend=default_backend,
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        required: bool = False,
        default: dict | None = None,
        default_backend: str | None = None,
    ) -> "ConnectionSettings":
        raw = read_json(path, default or {})
        return cls(
            raw if isinstance(raw, dict) else {},
            path=path,
            required=required,
            default_backend=default_backend,
        )

    # ── 属性 ──
    @property
    def is_reverse(self) -> bool:
        return self.type == MODE_REVERSE

    @property
    def is_forward(self) -> bool:
        return self.type == MODE_FORWARD

    @property
    def backend_label(self) -> str:
        return self.backend_profile.label

    @property
    def history_anchor(self) -> str:
        """历史接口游标字段：NapCat=message_seq，SnowLuma=message_id。"""
        return self.backend_profile.history_anchor

    @property
    def mode_host(self) -> str:
        return self.ws_reverse_host if self.is_reverse else self.ws_host

    @property
    def mode_port(self) -> int:
        return self.ws_reverse_port if self.is_reverse else self.ws_port

    @property
    def mode_label(self) -> str:
        return "反向(ws_re)" if self.is_reverse else "正向(ws)"

    @property
    def endpoint(self) -> str:
        return f"ws://{self.mode_host}:{self.mode_port}"

    @property
    def ws_url(self) -> str:
        return f"{self.endpoint}?access_token={self.access_token}"

    # 旧字段兼容
    @property
    def token(self) -> str:
        return self.access_token

    def describe(self) -> str:
        if self.is_reverse:
            return (
                f"反向 ws_re：监听 {self.endpoint}，等 {self.backend_label} 连过来"
            )
        return f"正向 ws：连接 {self.endpoint}（{self.backend_label}）"

    def raw(self) -> dict:
        return dict(self._raw)

    def to_dict(self) -> dict:
        data = dict(self._raw)
        data.update(
            {
                "type": self.type,
                "backend": self.backend,
                "access_token": self.access_token,
                "ws_host": self.ws_host,
                "ws_port": self.ws_port,
                "ws_reverse_host": self.ws_reverse_host,
                "ws_reverse_port": self.ws_reverse_port,
                "ws_reverse_strict_token": self.strict_token,
            }
        )
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ConnectionSettings(backend={self.backend!r}, type={self.type!r}, "
            f"endpoint={self.endpoint!r})"
        )


__all__ = [
    "MODE_FORWARD",
    "MODE_REVERSE",
    "DEFAULT_MODE",
    "DEFAULT_BACKEND",
    "ConnectionSettings",
    "normalize_mode",
]
